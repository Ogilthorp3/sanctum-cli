"""FIX-b: the box + Mini cutover transport is instance.yaml-config-driven.

The single-NAT DMZ cutover's keystone safety gate requires the OPERATOR HOST to be
OFF the 10.x LAN (so it survives a /1 collapse and recovers over Tailscale). But
the transport for the box (Firewalla) reads + writes and the Mini armor deploy was
hardcoded to LAN IPs (10.0.0.1 / bert@10.0.0.10), forcing the operator on-LAN.

FIX-b makes the box + Mini transport config-driven via three instance.yaml keys —
``devices.firewalla.host`` / ``devices.firewalla.ssh_user`` / ``devices.mini.host``
— read at CALL TIME and threaded into:

  (1) ``stage_armor`` — the armor kit's scp/ssh to the box + the Mini;
  (2) ``observe_lease`` / verify box reads — the Firewalla SSH runner;
  (3) the ``_DmzRollbackProvider`` recovery re-lease + recovery-verify.

These tests assert the CONTRACT, not the field (CLAUDE.md "Contracts at the
Boundary"): a config that pins a TAILNET host must make the *real produced
ssh/scp argv* target that host, and a no-config case must still target the shipped
LAN default. Expectations are authored from the CONSUMER — the actual
``ssh``/``scp`` invocation (``user@host``) the deploy/runner emits — never from the
producer's assumption. The cheap argv-construction boundary is RUN, not mocked;
only the genuinely-external edge (the subprocess exit/stdout, or the installer's
injected deploy runner) is stubbed, so the suite stays offline.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from sanctum_cli.commands import net as net_cmd
from sanctum_cli.devices import intents
from sanctum_cli.devices.base import OpResult, Snapshot

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# The TAILNET coordinates Bert pins for the off-LAN cutover perch.
TS_FW_HOST = "100.68.36.16"
TS_FW_USER = "pi"
TS_MINI = "bert@100.107.112.118"

# The shipped LAN defaults (general-purpose tool — unchanged for other users).
LAN_FW_HOST = "10.0.0.1"
LAN_MINI = "bert@10.0.0.10"


def _write_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tailnet: bool,
    key_file: Path | None = None,
) -> Path:
    """Write an instance.yaml (tailnet-pinned OR bare) + point the CLI at it.

    Isolated from the machine's real ``~/.sanctum/instance.yaml`` by setting
    ``SANCTUM_INSTANCE_FILE`` — the same isolation the existing config tests use.
    """
    cfg = tmp_path / "instance.yaml"
    body = "instance:\n  name: Test\n  slug: test\n"
    if tailnet:
        body += (
            "devices:\n"
            "  firewalla:\n"
            f"    host: {TS_FW_HOST}\n"
            f"    ssh_user: {TS_FW_USER}\n"
            "  mini:\n"
            f"    host: {TS_MINI}\n"
        )
    if key_file is not None:
        body += f"firewalla:\n  ssh_key: {key_file}\n"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(cfg))
    return cfg


class _ArgvRecordingDeployRunner:
    """Records every armor deploy argv; returns exit 0 (the cheap edge stub).

    The armor installer's ``_steps()``/``_armed_check_argv()`` argv construction is
    the consumer boundary under test — it is RUN, not mocked. Only the subprocess
    edge (the exit code) is stubbed, so the test stays offline.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        return 0


def _joined(calls: list[list[str]]) -> str:
    return "\n".join(" ".join(argv) for argv in calls)


# ── (1) stage_armor: the armor scp/ssh targets the configured box + Mini ──────


def test_build_armor_installer_targets_tailnet_box_and_mini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tailnet config → net._build_armor_installer().stage() scp/ssh the tailnet host.

    The contract: the CONFIGURED box + Mini hosts (not the hardcoded LAN IPs) reach
    the real ssh/scp argv the deploy emits.
    """
    _write_instance(tmp_path, monkeypatch, tailnet=True)
    rec = _ArgvRecordingDeployRunner()
    installer = net_cmd._build_armor_installer()
    installer._runner = rec.__call__  # type: ignore[attr-defined]  # stub the subprocess edge
    res = installer.stage()
    assert res.ok is True
    joined = _joined(rec.calls)
    assert f"{TS_FW_USER}@{TS_FW_HOST}" in joined  # the box over the tailnet
    assert TS_MINI in joined  # the Mini over the tailnet
    # The LAN default must NOT leak in when the tailnet host is pinned.
    assert f"@{LAN_FW_HOST}" not in joined
    assert LAN_MINI not in joined


def test_build_armor_installer_defaults_to_lan_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No devices.firewalla/mini config → the shipped LAN defaults still target 10.0.0.1."""
    _write_instance(tmp_path, monkeypatch, tailnet=False)
    rec = _ArgvRecordingDeployRunner()
    installer = net_cmd._build_armor_installer()
    installer._runner = rec.__call__  # type: ignore[attr-defined]
    installer.stage()
    joined = _joined(rec.calls)
    assert f"pi@{LAN_FW_HOST}" in joined
    assert LAN_MINI in joined
    assert TS_FW_HOST not in joined


def test_intents_default_armor_installer_is_config_driven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The intents fallback default installer is ALSO config-driven (single source).

    ``intents._default_armor_installer`` is the seam reached when a caller omits an
    injected ``armor=``; it must honor the same instance.yaml pins, not a hardcode.
    """
    _write_instance(tmp_path, monkeypatch, tailnet=True)
    rec = _ArgvRecordingDeployRunner()
    installer = intents._default_armor_installer()
    installer._runner = rec.__call__  # type: ignore[attr-defined]
    installer.stage()
    joined = _joined(rec.calls)
    assert f"{TS_FW_USER}@{TS_FW_HOST}" in joined
    assert TS_MINI in joined


# ── (2) observe_lease / verify box reads: the runner SSHes the configured box ──


def _capture_ssh_runner(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch the Firewalla SSH subprocess edge to record argv; return the record."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        captured.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("sanctum_cli.net.system.subprocess.run", fake_run)
    return captured


def test_build_runner_box_reads_target_tailnet_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tailnet config → the runner's lease_observe/dhcp_release SSH pi@<tailnet>.

    observe_lease + the recovery re-lease both ride the runner ``_build_runner``
    produces; pinning the box host must retarget the real ssh argv they emit.
    """
    key = tmp_path / "ssh_firewalla"
    key.write_text("KEY", encoding="utf-8")
    _write_instance(tmp_path, monkeypatch, tailnet=True, key_file=key)
    captured = _capture_ssh_runner(monkeypatch)

    runner = net_cmd._build_runner()
    runner(("lease_observe",))
    runner(("dhcp_release",))

    joined = _joined(captured)
    assert f"pi@{TS_FW_HOST}" in joined
    assert f"pi@{LAN_FW_HOST}" not in joined


def test_build_runner_box_reads_default_to_detected_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No box-host config → the runner SSHes the detected LAN gateway (10.0.0.1).

    The shipped general-purpose default is gateway-derived (10.0.0.x on the LAN) —
    unchanged for other users; Bert's tailnet pin is purely additive.
    """
    key = tmp_path / "ssh_firewalla"
    key.write_text("KEY", encoding="utf-8")
    _write_instance(tmp_path, monkeypatch, tailnet=False, key_file=key)
    captured = _capture_ssh_runner(monkeypatch)
    monkeypatch.setattr(net_cmd.detect, "parse_default_gateway", lambda _out: LAN_FW_HOST)

    runner = net_cmd._build_runner()
    runner(("dhcp_release",))

    joined = _joined(captured)
    assert f"pi@{LAN_FW_HOST}" in joined
    assert TS_FW_HOST not in joined


def test_firewalla_recovery_host_honors_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery gate's box host resolves config-first (same box the re-lease hits).

    The gate must probe the SAME box the ``dhcp_release`` re-lease reaches; pinning
    the tailnet host must move BOTH together, or the gate would probe the wrong box.
    """
    _write_instance(tmp_path, monkeypatch, tailnet=True)
    assert net_cmd._firewalla_recovery_host() == TS_FW_HOST


# ── (3) recovery re-lease: _DmzRollbackProvider SSHes the configured box ───────


class _FakeInner:
    """A minimal inner provider for the rollback wrapper: disable + reboot succeed."""

    brand = "fake-hub"
    kind = "hub"

    def rollback(self, _snap: Snapshot) -> OpResult:
        return OpResult(ok=True, detail="DMZ disabled")

    def reboot(self) -> OpResult:
        return OpResult(ok=True, detail="rebooted")


def test_recovery_release_targets_tailnet_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The _DmzRollbackProvider recovery re-lease SSHes the CONFIGURED box.

    The unwind's ``dhcp_release`` (identifiable by its ``dhclient`` remote) must
    target the tailnet box when pinned — so an off-LAN operator's rollback reaches
    the Firewalla over Tailscale instead of a dead 10.x address.
    """
    key = tmp_path / "ssh_firewalla"
    key.write_text("KEY", encoding="utf-8")
    _write_instance(tmp_path, monkeypatch, tailnet=True, key_file=key)
    captured = _capture_ssh_runner(monkeypatch)

    runner = net_cmd._build_runner()
    wrapped = intents._DmzRollbackProvider(_FakeInner(), runner)  # type: ignore[arg-type]
    wrapped.rollback(Snapshot(brand="fake-hub", taken_at="t", data={"d": "off"}))

    # The re-lease step (dhclient remote) must have SSHed the tailnet box.
    release_argv = [argv for argv in captured if any("dhclient" in a for a in argv)]
    assert release_argv, "recovery must fire the dhcp_release re-lease over SSH"
    assert all(f"pi@{TS_FW_HOST}" in " ".join(argv) for argv in release_argv)
    assert f"pi@{LAN_FW_HOST}" not in _joined(captured)
