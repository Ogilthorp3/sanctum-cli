"""``SinglenatArmorInstaller`` — the concrete armor-kit installer seam.

The ``apply_armor`` stage of the :func:`sanctum_cli.devices.intents.single_nat_dmz`
cutover installs the ``sanctum-singlenat-armor`` kit onto the Firewalla (via the
Mini jump host) and arms the watchdog/sentinel launch agents on the Mini, exactly
as the kit's ``README.md`` deploy section prescribes. The orchestrator drives it
through the :class:`~sanctum_cli.devices.intents.ArmorInstaller` Protocol so the
install is mockable; this module is the *real* implementation behind that seam.

These tests author their expectations from the **consumer's contract** (the
kit's README deploy steps + the orchestrator's ``ArmorInstaller`` Protocol +
``OpResult`` shape) — never from the production module's own assumptions
(Contracts at the Boundary). The genuinely-dangerous edge (a real ``scp``/``ssh``
to a live Firewalla + ``launchctl bootstrap`` on the Mini) is the ONLY thing
mocked, via an injected command runner that records every argv and returns a
scripted exit code; a happy-path install must drive the real ordered step
sequence through it, and a hostile (non-zero / raising) step must fail-closed —
the bug this seam exists to prevent is a swallowed install that reports a green
cutover while the armor never landed.
"""

from __future__ import annotations

from sanctum_cli.devices.base import OpResult
from sanctum_cli.devices.intents import ArmorInstaller


class RecordingRunner:
    """Records every deploy command argv; returns scripted exit codes.

    The real installer shells out to ``scp``/``ssh``/``launchctl``; this fake is
    that subprocess boundary's stand-in. It records each argv (so the ordered
    deploy sequence can be asserted) and returns ``0`` by default. A test may
    script a non-zero exit for the Nth call (``fail_on_index``) or make a call
    raise (``raise_on_index``) to exercise the fail-closed forks — the two ways a
    real ``subprocess.run`` signals a failed step (non-zero return vs OSError).
    """

    def __init__(
        self,
        *,
        fail_on_index: int | None = None,
        raise_on_index: int | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._fail_on = fail_on_index
        self._raise_on = raise_on_index

    def __call__(self, argv: list[str]) -> int:
        idx = len(self.calls)
        self.calls.append(list(argv))
        if self._raise_on is not None and idx == self._raise_on:
            msg = "scp: connection refused"
            raise OSError(msg)
        if self._fail_on is not None and idx == self._fail_on:
            return 1
        return 0


def _installer(runner: RecordingRunner):
    from sanctum_cli.devices.armor import SinglenatArmorInstaller

    return SinglenatArmorInstaller(
        kit_dir="/Users/bert/Documents/Claude_Code/sanctum-singlenat-armor",
        firewalla_host="10.0.0.1",
        mini_host="bert@10.0.0.10",
        runner=runner.__call__,
    )


# ── structural: the real installer satisfies the orchestrator's Protocol ─────


def test_installer_satisfies_armor_installer_protocol() -> None:
    """The concrete installer is structurally an ``ArmorInstaller`` (the seam type).

    The orchestrator narrows ``armor`` to the ``ArmorInstaller`` Protocol; the real
    implementation must conform so it drops straight into the ``apply_armor`` stage.
    """
    inst = _installer(RecordingRunner())
    assert isinstance(inst, ArmorInstaller)


# ── happy path: drives the real ordered deploy sequence, returns ok=True ─────


def test_install_runs_deploy_sequence_and_reports_ok() -> None:
    """A clean install fires the README deploy steps in order and reports ok=True.

    Authored from the kit README's deploy section, NOT the module's own list:
      1. scp the boot-armor to the Firewalla (via the Mini jump host);
      2. ssh the Firewalla to append post_main.sh + run the boot-armor once;
      3. scp the lib + bin + plists onto the Mini;
      4. launchctl bootstrap the watchdog + ota-sentinel launch agents on the Mini.
    """
    runner = RecordingRunner()
    res = _installer(runner).install()

    assert isinstance(res, OpResult)
    assert res.ok is True

    # It actually drove the deploy (no silent no-op masquerading as success).
    assert runner.calls, "install() must run the deploy steps, not no-op"
    flat = [" ".join(argv) for argv in runner.calls]
    joined = "\n".join(flat)

    # The Firewalla boot-armor lands + post_main.sh wiring + a one-shot run.
    assert any("scp" in c and "singlenat-armor-boot.sh" in c for c in flat)
    assert "post_main.sh" in joined
    # The Mini gets the lib/bin/plists + the watchdog AND sentinel arms.
    assert "launchctl" in joined
    assert "com.sanctum.singlenat-watchdog" in joined
    assert "com.sanctum.singlenat-ota-sentinel" in joined


def test_install_targets_the_configured_hosts() -> None:
    """The deploy is parameterized on the Firewalla + Mini hosts (no hardcode)."""
    runner = RecordingRunner()
    _installer(runner).install()
    joined = "\n".join(" ".join(argv) for argv in runner.calls)
    assert "10.0.0.1" in joined  # Firewalla
    assert "bert@10.0.0.10" in joined  # Mini jump host


# ── fail-closed: a failed step reports ok=False, NEVER a green cutover ────────


def test_install_fails_closed_on_nonzero_step() -> None:
    """A deploy step returning non-zero fails-closed: ok=False, never silent green.

    The whole reason the seam exists (Contracts at the Boundary): a swallowed
    install would report a green cutover while the armor never landed and the
    rails' rollback would never fire.
    """
    runner = RecordingRunner(fail_on_index=0)  # the very first scp fails
    res = _installer(runner).install()
    assert res.ok is False
    assert res.detail  # names the failed step


def test_install_fails_closed_when_a_step_raises() -> None:
    """A deploy step whose subprocess RAISES (scp: connection refused) → ok=False.

    Mirrors the sagemcom provider's fail-closed convention: a transport that
    cannot even spawn is a failed install, surfaced as ok=False (not a raise that
    escapes the seam) so guarded_apply treats it like any other failed stage.
    """
    runner = RecordingRunner(raise_on_index=1)
    res = _installer(runner).install()
    assert res.ok is False
    assert res.detail


def test_install_stops_at_the_first_failed_step() -> None:
    """Fail-closed is also fail-FAST: a failed step short-circuits the remainder.

    Once a step fails the install is already doomed (the armor is half-deployed);
    continuing to scp/launchctl past it would only widen the blast radius. The
    step that fails must be the LAST command the runner sees.
    """
    runner = RecordingRunner(fail_on_index=1)  # second step fails
    res = _installer(runner).install()
    assert res.ok is False
    # Exactly the steps up to and including the failed one ran — no further calls.
    assert len(runner.calls) == 2


# ── default runner: the real seam shells out (constructed without injection) ──


def test_default_installer_uses_a_real_subprocess_runner() -> None:
    """Constructed without a runner, the installer wires a real subprocess seam.

    Proves the production path has a runner (so a real cutover actually shells
    out) — we do NOT call .install() here (that would scp/ssh a live box), only
    that the default runner is a callable the seam will drive.
    """
    from sanctum_cli.devices.armor import SinglenatArmorInstaller

    inst = SinglenatArmorInstaller(
        kit_dir="/Users/bert/Documents/Claude_Code/sanctum-singlenat-armor",
        firewalla_host="10.0.0.1",
        mini_host="bert@10.0.0.10",
    )
    assert callable(inst._runner)  # type: ignore[attr-defined]
