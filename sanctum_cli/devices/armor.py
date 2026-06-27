"""The concrete single-NAT armor-kit installer — the ``apply_armor`` stage's seam.

The :func:`sanctum_cli.devices.intents.single_nat_dmz` cutover engages Bell
Advanced DMZ + ``/32`` on the Firewalla Gold Pro — an unsupported firerouter-layer
change that an OTA can silently wipe. The ``sanctum-singlenat-armor`` kit is the
self-healing safety net for that hack (a boot-persistent DHCP hook + a Mini-side
watchdog + an OTA sentinel); installing it is stage ``apply_armor`` of the flip.

This module is the *real* implementation behind the orchestrator's
:class:`~sanctum_cli.devices.intents.ArmorInstaller` Protocol. It is a **thin
seam**: it shells out the kit ``README.md``'s deploy steps in order — ``scp`` the
boot-armor to the Firewalla (via the Mini jump host), ``ssh`` it to wire
``post_main.sh`` + run the boot-armor once, ``scp`` the lib/bin/plists onto the
Mini, and ``launchctl bootstrap`` the watchdog + OTA-sentinel launch agents — and
returns an :class:`~sanctum_cli.devices.base.OpResult`. All real I/O lives here;
the pure decision/sequencing brain is :mod:`sanctum_cli.devices.flip`.

**Fail-closed, exactly like the Sagemcom provider's ``set``/``reboot``** (Contracts
at the Boundary): a deploy step that returns a non-zero exit code — OR whose
subprocess cannot even spawn (``scp: connection refused``) — yields
``OpResult(ok=False, ...)`` naming the failed step, never a silent green. A
swallowed install would otherwise report a green cutover while the armor never
landed and the rails' rollback would never fire. The install is also fail-FAST:
the first failed step short-circuits the rest, so a half-deployed armor never
fans out further scp/launchctl calls.

The subprocess is driven through an injectable ``runner`` (``list[str] -> int``,
the step's exit code) so tests record every argv and script exit codes without
ever touching a live Firewalla / the Mini's ``launchctl``; the default runner is
the real :func:`_subprocess_runner`. Nothing here fires on import — a caller must
construct the installer and call :meth:`SinglenatArmorInstaller.install`, which
the orchestrator only reaches on an ``apply=True`` (never the dry-run) path.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from sanctum_cli.devices.base import OpResult

# A deploy-command runner: maps a step's argv to its process exit code (0 == ok).
# Mirrors the net layer's subprocess seam (see sanctum_cli.net.system._run) — the
# thin boundary tests inject a recording double over.
DeployRunner = Callable[[list[str]], int]

# A confirm-command runner: maps the README step-3 verify argv to its STDOUT (the
# verify.sh JSON line). DISTINCT from DeployRunner because the deploy steps only
# care about an exit code, but the confirm gate must PARSE the script's
# ``{"state":"HEALTHY",...}`` output — an exit-code-only runner cannot tell a
# HEALTHY armor from a DEGRADED one. A transport that cannot spawn raises (so the
# fail-closed path treats "no confirm output" as "not proven healthy").
ConfirmRunner = Callable[[list[str]], str]

# The kit README's step-3 confirm: run ``singlenat-verify.sh`` on the Mini and read
# its one-line ``{"state":"HEALTHY",...}`` verdict. The path mirrors the README's
# "Layout on the Mini" (the verify script lands in ~/.sanctum/bin alongside the
# other bin scripts the installer scp'd there).
_MINI_VERIFY_SCRIPT = "~/.sanctum/bin/singlenat-verify.sh"

# The ONLY ``state`` token in the verify.sh JSON that means the armor came up
# healthy (the script exits 0 for this, 1 for degraded/dark). Authored from the
# script's own ``rollup_state`` HEALTHY verdict, not assumed.
_HEALTHY_STATE = "HEALTHY"

# How long any single deploy step (scp/ssh/launchctl) may take before we treat it
# as a failed step. A 2 a.m. attended cutover must not hang forever on a dead
# transport — a timeout is a non-zero outcome the fail-closed path surfaces.
_STEP_TIMEOUT = 60

# Where the kit's deployables land on the Mini, per the README's "Layout on the
# Mini" note: lib at ~/.sanctum/lib, bin at ~/.sanctum/bin (siblings so the bin
# scripts resolve the lib via $HERE/../lib), plists in ~/Library/LaunchAgents.
_MINI_LIB = "~/.sanctum/lib/"
_MINI_BIN = "~/.sanctum/bin/"
_MINI_LAUNCH_AGENTS = "~/Library/LaunchAgents/"

# The two launch agents the kit arms on the Mini (the README's step 2 bootstrap).
_WATCHDOG_PLIST = "com.sanctum.singlenat-watchdog.plist"
_SENTINEL_PLIST = "com.sanctum.singlenat-ota-sentinel.plist"

# Where the boot-armor lands on the Firewalla + the user hook it is wired into.
_FW_ARMOR_DEST = "/home/pi/.firewalla/config/sanctum-singlenat-armor.sh"
_FW_POST_MAIN = "/home/pi/.firewalla/config/post_main.sh"


def _subprocess_runner(argv: list[str]) -> int:
    """Run one deploy step, returning its exit code; 1 if it could not spawn.

    The real boundary the injected ``runner`` stands in for in tests. Never
    raises — a transport that cannot spawn (``OSError``) or a step that overruns
    :data:`_STEP_TIMEOUT` is reported as a non-zero exit code, so the fail-closed
    path in :meth:`SinglenatArmorInstaller.install` treats it like any other
    failed step rather than letting an exception escape the seam.
    """
    try:
        # argv is built from module constants + configured hosts, never a shell string.
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_STEP_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 1
    return proc.returncode


def _subprocess_confirm_runner(argv: list[str]) -> str:
    """Run the README confirm step, returning its STDOUT; RAISE on transport failure.

    Unlike :func:`_subprocess_runner` (exit-code only), the confirm needs the
    verify.sh JSON line, so this returns stdout. It deliberately RAISES (rather than
    swallowing into "") when the subprocess cannot spawn / times out / exits
    non-zero: a confirm that did not produce a verdict is the ABSENCE of proof the
    armor is healthy, and :meth:`SinglenatArmorInstaller.install` turns a raised
    confirm into a fail-closed ``ok=False`` (never a blind green). The verify.sh
    exit code (0 HEALTHY / 1 degraded) is a non-zero on any non-healthy verdict —
    but we DON'T trust the exit code alone here: install parses the JSON ``state``,
    so a non-zero exit (degraded) is surfaced through the parsed state, while a
    spawn/timeout failure raises.
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_STEP_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        msg = f"confirm step could not run: {exc}"
        raise OSError(msg) from exc
    return proc.stdout


class SinglenatArmorInstaller:
    """Installs the ``sanctum-singlenat-armor`` kit per its README — fail-closed.

    Constructed with the kit checkout dir + the Firewalla and Mini hosts; its
    :meth:`install` drives the README's ordered deploy steps through the injected
    ``runner`` and returns an :class:`OpResult`. Structurally an
    :class:`~sanctum_cli.devices.intents.ArmorInstaller`, so it drops straight into
    the orchestrator's ``apply_armor`` stage.
    """

    def __init__(
        self,
        *,
        kit_dir: str,
        firewalla_host: str,
        mini_host: str,
        firewalla_user: str = "pi",
        runner: DeployRunner | None = None,
        confirm_runner: ConfirmRunner | None = None,
    ) -> None:
        """Capture the deploy coordinates; default to real subprocess runners.

        ``kit_dir`` is the local ``sanctum-singlenat-armor`` checkout (source of
        the scp'd files); ``firewalla_host``/``firewalla_user`` and ``mini_host``
        (an ``ssh``-shaped ``user@host``) are the deploy targets. ``runner``
        defaults to :func:`_subprocess_runner` so a constructed-without-injection
        installer really shells out; ``confirm_runner`` defaults to
        :func:`_subprocess_confirm_runner` so the README step-3 verify really runs
        (it returns the verify.sh STDOUT so ``install`` can parse the JSON state).
        Tests inject recording doubles for both.
        """
        self._kit_dir = kit_dir.rstrip("/")
        self._fw_host = firewalla_host
        self._fw_user = firewalla_user
        self._mini = mini_host
        self._runner: DeployRunner = runner or _subprocess_runner
        self._confirm_runner: ConfirmRunner = confirm_runner or _subprocess_confirm_runner

    def _steps(self) -> list[tuple[str, list[str]]]:
        """The ordered (name, argv) deploy steps, authored from the kit README.

        Each step is a single ``scp``/``ssh``/``launchctl`` invocation; the names
        are surfaced verbatim in a fail-closed ``OpResult.detail`` so an operator
        knows exactly which step of the cutover broke.
        """
        fw = f"{self._fw_user}@{self._fw_host}"
        kit = self._kit_dir
        # README step 1: land the boot-armor on the Firewalla, wire post_main.sh,
        # run it once. The wire+run is one ssh so the append is idempotent (grep -q).
        wire_and_run = (
            f"grep -q sanctum-singlenat-armor {_FW_POST_MAIN} "
            f'|| echo "bash {_FW_ARMOR_DEST}" >> {_FW_POST_MAIN}; '
            f"sudo bash {_FW_ARMOR_DEST}"
        )
        # README step 2: lib + bin + plists onto the Mini, then bootstrap the two
        # launch agents. mkdir first so the scp targets exist.
        bootstrap = (
            f"launchctl bootstrap gui/$(id -u) {_MINI_LAUNCH_AGENTS}{_WATCHDOG_PLIST} "
            f"&& launchctl bootstrap gui/$(id -u) {_MINI_LAUNCH_AGENTS}{_SENTINEL_PLIST}"
        )
        return [
            (
                "scp boot-armor → Firewalla",
                ["scp", f"{kit}/bin/singlenat-armor-boot.sh", f"{fw}:{_FW_ARMOR_DEST}"],
            ),
            (
                "wire post_main.sh + run boot-armor",
                ["ssh", fw, wire_and_run],
            ),
            (
                "mkdir Mini lib/bin",
                ["ssh", self._mini, f"mkdir -p {_MINI_BIN} {_MINI_LIB}"],
            ),
            (
                "scp eval lib → Mini",
                ["scp", f"{kit}/lib/singlenat-eval.sh", f"{self._mini}:{_MINI_LIB}"],
            ),
            (
                "scp bin scripts → Mini",
                ["scp", f"{kit}/bin/singlenat-armor-boot.sh",
                 f"{kit}/bin/singlenat-watchdog.sh",
                 f"{kit}/bin/singlenat-ota-sentinel.sh",
                 f"{kit}/bin/singlenat-verify.sh",
                 f"{self._mini}:{_MINI_BIN}"],
            ),
            (
                "scp launch agents → Mini",
                ["scp",
                 f"{kit}/launchd/{_WATCHDOG_PLIST}",
                 f"{kit}/launchd/{_SENTINEL_PLIST}",
                 f"{self._mini}:{_MINI_LAUNCH_AGENTS}"],
            ),
            (
                "bootstrap watchdog + ota-sentinel",
                ["ssh", self._mini, bootstrap],
            ),
        ]

    def _confirm_argv(self) -> list[str]:
        """The README step-3 confirm: ``ssh <mini> '<verify-script>'``.

        Runs the kit's ``singlenat-verify.sh`` on the Mini and (via the confirm
        runner) yields its ``{"state":"HEALTHY",...}`` JSON line for parsing.
        """
        return ["ssh", self._mini, _MINI_VERIFY_SCRIPT]

    def _armed_check_argv(self) -> list[str]:
        """The PRE-DMZ structural armed check: is the /32 hook present + wired on the box?

        Distinct from :meth:`_confirm_argv` (the post-cutover HEALTHY egress confirm,
        which CANNOT pass before single-NAT is live). This is a STRUCTURAL proof the
        deploy landed: the boot-armor hook file exists + is executable on the
        Firewalla AND ``post_main.sh`` is wired to run it — so the supersede will
        fire the instant the post-reboot poison /1 lease arrives. Exit 0 == armed.
        """
        fw = f"{self._fw_user}@{self._fw_host}"
        remote = (
            f"test -x {_FW_ARMOR_DEST} && grep -q sanctum-singlenat-armor {_FW_POST_MAIN}"
        )
        return ["ssh", fw, remote]

    def stage(self) -> OpResult:
        """PRE-DMZ (FIX-2): deploy the kit + STRUCTURALLY arm it, fail-closed + fail-fast.

        Runs the SAME ordered :meth:`_steps` deploy as :meth:`install` (idempotent —
        ``grep -q`` guards the post_main wire, ``scp`` overwrites, the boot-armor run
        is re-entrant) so the ``/32`` DHCP-supersede hook + MTU clamp land on the box
        WHILE the LAN is still healthy and the box is reachable. It then runs a
        STRUCTURAL armed check (:meth:`_armed_check_argv`) — the hook file is present +
        executable + wired into ``post_main.sh`` — NOT the HEALTHY egress confirm,
        which cannot pass before single-NAT is live.

        This is the stage that closes the 2026-06-26 un-armored window: with the
        supersede already installed, the instant the post-reboot DMZ lease arrives
        carrying Bell's poison ``/1`` it is pinned to ``/32`` + on-link gateway,
        instead of collapsing the ``10.x`` LAN. Fail-closed (any non-zero/raising
        step → ``ok=False``) and fail-FAST (the first failed step short-circuits) so
        a half-deployed armor never fans out further calls — and the rails roll the
        flip back BEFORE Advanced DMZ is ever engaged.
        """
        steps = self._steps()
        for name, argv in steps:
            try:
                code = self._runner(argv)
            except Exception as exc:  # a raising runner must fail-closed, not escape the seam
                return OpResult(
                    ok=False,
                    detail=f"armor stage failed at step {name!r} (raised: {exc})",
                )
            if code != 0:
                return OpResult(
                    ok=False,
                    detail=f"armor stage failed at step {name!r} (exit {code})",
                )
        # Every deploy step landed. STRUCTURAL armed check (NOT the HEALTHY egress
        # confirm — single-NAT is not live yet): the /32 hook must be present + wired.
        try:
            armed = self._runner(self._armed_check_argv())
        except Exception as exc:
            return OpResult(
                ok=False,
                detail=f"armor stage armed-check could not run: {exc}",
            )
        if armed != 0:
            return OpResult(
                ok=False,
                detail=(
                    "armor stage: the /32 hook is NOT armed on the box "
                    "(hook-file / post_main.sh wiring check failed) — refusing to "
                    "engage DMZ over an un-armored WAN"
                ),
            )
        return OpResult(
            ok=True,
            detail=f"armor staged + armed ({len(steps)} deploy steps, /32 hook wired)",
        )

    def _confirm_healthy(self) -> OpResult | None:
        """Run the README confirm gate; return an ``ok=False`` OpResult, or None if HEALTHY.

        Fires the verify SSH, parses the verify.sh JSON ``state``, and returns:

        * ``None`` when ``state == "HEALTHY"`` — the armor genuinely came up; the
          caller proceeds to report ok=True;
        * an ``ok=False`` OpResult otherwise — a confirm transport that could not
          run (raised), output that is not the expected JSON (the absence of a
          verdict), or a non-HEALTHY ``state`` (the armor came up degraded/dark).

        This is the honest-verify gate (Contracts at the Boundary): a green install
        derives from the consumer's REAL HEALTHY verdict, never from "the deploy
        commands all exited zero" — a scp can succeed while the watchdog never
        comes up healthy.
        """
        argv = self._confirm_argv()
        try:
            out = self._confirm_runner(argv)
        except Exception as exc:  # a confirm that cannot run is the ABSENCE of proof
            return OpResult(
                ok=False,
                detail=f"armor install confirm step could not run: {exc}",
            )
        try:
            verdict = json.loads(out)
            state = verdict["state"] if isinstance(verdict, dict) else None
        except (ValueError, TypeError, KeyError):
            state = None
        if state is None:
            return OpResult(
                ok=False,
                detail=(
                    "armor install confirm step returned no parseable verify verdict "
                    f"(expected a JSON line with a 'state'; got: {out.strip()[:120]!r})"
                ),
            )
        if state != _HEALTHY_STATE:
            return OpResult(
                ok=False,
                detail=(
                    f"armor install confirm reported state {state!r}, not {_HEALTHY_STATE!r} — "
                    "the cutover did not land a healthy single NAT"
                ),
            )
        return None

    def install(self) -> OpResult:
        """Deploy the armor kit, fail-closed + fail-fast, then CONFIRM it is HEALTHY.

        Walks :meth:`_steps` in order through the runner. The FIRST step with a
        non-zero exit code short-circuits the rest and returns
        ``OpResult(ok=False, ...)`` naming that step (the half-deployed armor must
        not fan out further calls — and the confirm gate below must NOT run on top
        of a half-deployed armor). Only when EVERY deploy step exited zero does the
        install run the README's step-3 confirm: ``ssh <mini> singlenat-verify.sh``
        → parse the JSON ``state``. A non-HEALTHY verdict, an unparseable output, or
        a confirm transport that could not run each yields ``ok=False`` — so a green
        install is only ever reported when the armor genuinely came up HEALTHY, not
        merely because the scp/launchctl commands exited zero (honest-verify).
        """
        steps = self._steps()
        for name, argv in steps:
            try:
                code = self._runner(argv)
            except Exception as exc:  # a raising runner must fail-closed, not escape the seam
                # The default runner already swallows transport errors into a
                # non-zero code, but an injected runner (or a future transport)
                # could raise — and a raised step is still a failed step. Treat it
                # exactly like a non-zero exit so no exception escapes the seam to
                # crash the cutover instead of tripping the rails' rollback.
                return OpResult(
                    ok=False,
                    detail=f"armor install failed at step {name!r} (raised: {exc})",
                )
            if code != 0:
                return OpResult(
                    ok=False,
                    detail=f"armor install failed at step {name!r} (exit {code})",
                )
        # Every deploy step landed. Now the README step-3 confirm is the gate: only
        # a HEALTHY verify verdict lets the install report ok=True (never blind).
        confirm_failure = self._confirm_healthy()
        if confirm_failure is not None:
            return confirm_failure
        return OpResult(
            ok=True,
            detail=f"armor kit installed + confirmed HEALTHY ({len(steps)} deploy steps)",
        )
