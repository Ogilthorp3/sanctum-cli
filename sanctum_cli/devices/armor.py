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

import subprocess
from collections.abc import Callable

from sanctum_cli.devices.base import OpResult

# A deploy-command runner: maps a step's argv to its process exit code (0 == ok).
# Mirrors the net layer's subprocess seam (see sanctum_cli.net.system._run) — the
# thin boundary tests inject a recording double over.
DeployRunner = Callable[[list[str]], int]

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
    ) -> None:
        """Capture the deploy coordinates; default to a real subprocess runner.

        ``kit_dir`` is the local ``sanctum-singlenat-armor`` checkout (source of
        the scp'd files); ``firewalla_host``/``firewalla_user`` and ``mini_host``
        (an ``ssh``-shaped ``user@host``) are the deploy targets. ``runner``
        defaults to :func:`_subprocess_runner` so a constructed-without-injection
        installer really shells out; tests inject a recording double.
        """
        self._kit_dir = kit_dir.rstrip("/")
        self._fw_host = firewalla_host
        self._fw_user = firewalla_user
        self._mini = mini_host
        self._runner: DeployRunner = runner or _subprocess_runner

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

    def install(self) -> OpResult:
        """Deploy the armor kit, fail-closed + fail-fast.

        Walks :meth:`_steps` in order through the runner. The FIRST step with a
        non-zero exit code short-circuits the rest and returns
        ``OpResult(ok=False, ...)`` naming that step (the half-deployed armor must
        not fan out further calls). Only an all-steps-zero run returns
        ``OpResult(ok=True, ...)`` — so a swallowed/partial install can never
        report the green cutover the rails would commit on.
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
        return OpResult(
            ok=True,
            detail=f"armor kit installed ({len(steps)} deploy steps)",
        )
