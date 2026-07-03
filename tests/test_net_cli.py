from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands.net import _firewalla_key_path
from sanctum_cli.net import heal
from tests.net import fixtures as fx

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


# ─── Firewalla SSH key resolution (discovery-first) ──────────────────


def test_firewalla_key_path_honors_instance_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured firewalla.ssh_key in instance.yaml wins over the default."""
    inst = tmp_path / "instance.yaml"
    custom = tmp_path / ".ssh" / "my_fw_key"
    inst.write_text(f"firewalla:\n  ssh_key: {custom}\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    assert _firewalla_key_path() == custom


def test_firewalla_key_path_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured key path with ~ is expanded to the user's home."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("firewalla:\n  ssh_key: ~/.ssh/custom_fw\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    assert _firewalla_key_path() == Path("~/.ssh/custom_fw").expanduser()


def test_firewalla_key_path_defaults_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no firewalla.ssh_key set, the back-compat default applies."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    assert _firewalla_key_path() == Path.home() / ".ssh" / "firewalla_ed25519"


def test_net_check_reports_double_nat() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.DOUBLE_NAT)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "Bell")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=True),
    ):
        result = runner.invoke(app, ["net", "check"])
    assert result.exit_code == 0, result.stdout
    assert "double" in result.stdout.lower()


def test_net_check_single_nat_says_optimal() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.SINGLE_NAT)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "Bell")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=True),
    ):
        result = runner.invoke(app, ["net", "check"])
    assert result.exit_code == 0
    assert "optimal" in result.stdout.lower() or "already" in result.stdout.lower()


def test_net_optimize_not_applicable_no_firewalla_exits_clean() -> None:
    with (
        patch(
            "sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.NO_FIREWALLA)
        ),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=False),
    ):
        result = runner.invoke(app, ["net", "optimize", "--yes"])
    assert result.exit_code == 0
    assert "nothing to optimize" in result.stdout.lower()


def test_net_optimize_double_nat_prints_plan() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.DOUBLE_NAT)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "Bell")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=True),
    ):
        result = runner.invoke(app, ["net", "optimize", "--yes", "--plan-only"])
    assert result.exit_code == 0, result.stdout
    assert "20:6d:31:51:67:82" in result.stdout
    assert "Advanced DMZ" in result.stdout


# ─── speedtest ───────────────────────────────────────────────────────

# A fake host runner: 2.5 GbE wired link, no Firewalla port data.
SPEEDTEST_WIRED: dict[tuple[str, ...], str] = {
    ("route",): "  interface: en7\n  gateway: 10.0.0.1\n",
    ("link_speed",): "\tmedia: autoselect (2500Base-T <full-duplex>)\n\tstatus: active\n",
    ("airport_ports",): "Hardware Port: Ethernet\nDevice: en7\n",
}


class _SpeedFakeRunner:
    def __init__(self, table: dict[tuple[str, ...], str]) -> None:
        self._table = table
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        return self._table.get(tag, "")


def test_net_speedtest_no_test_json_is_parseable_and_skips_download() -> None:
    import json

    fake = _SpeedFakeRunner(SPEEDTEST_WIRED)
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fake),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=False),
    ):
        result = runner.invoke(app, ["net", "speedtest", "--no-test", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["multi_gbps"] is None  # no live test ran
    assert payload["single_gbps"] is None
    assert payload["ceiling_gbps"] == 2.5
    assert payload["on_wifi"] is False
    # The download tag must never be requested in --no-test mode.
    assert ("live_test",) not in fake.calls


def test_net_speedtest_no_test_human_output() -> None:
    fake = _SpeedFakeRunner(SPEEDTEST_WIRED)
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fake),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=False),
    ):
        result = runner.invoke(app, ["net", "speedtest", "--no-test"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "ceiling" in out
    assert "nat" in out


# ─── net heal (Task 4 — dry-run + guarded --apply) ───────────────────
#
# The heal CLI reads posture via an injected CommandRunner (argv -> stdout), so
# NO live networksetup/ipconfig/route/ping/sudo is ever touched here. The tests
# patch ``sanctum_cli.commands.net._build_heal_runner`` to feed canned outputs,
# and (for --apply) ``os.getuid`` so the root gate is exercised deterministically.


class _HealFakeRunner:
    """A substring-keyed, mutation-aware fake CommandRunner (argv: list[str]).

    ``table`` maps a substring of the joined argv to the stdout to return (first
    match wins), mirroring the pure-core test ``_run`` helper. When a healing
    mutation fires (``-setdhcp`` / ``set en1 DHCP``) the runner switches to
    ``after`` — so a test can simulate a heal that WORKS (gateway comes back) or
    STAYS BROKEN (gateway still dead → the CLI must revert). Every argv is logged
    so a test can assert whether a mutating / reverting command was issued.
    """

    def __init__(
        self, before: dict[str, str], after: dict[str, str] | None = None
    ) -> None:
        self._before = before
        self._after = after if after is not None else before
        self._table = before
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        joined = " ".join(argv)
        if "-setdhcp" in joined or "set en1 DHCP" in joined:
            # A healing mutation fired — subsequent posture reads see `after`.
            self._table = self._after
        for pat, out in self._table.items():
            if pat in joined:
                return out
        return ""


# A Manual/static Mini on a foreign LAN with the tailnet spine up — STATIC_DRIFT.
_STATIC_DRIFT_BEFORE: dict[str, str] = {
    "listallhardwareports": "Hardware Port: Wi-Fi\nDevice: en1\nEthernet Address: d0:11:e5:1c:88:59",
    "getsummary en1": "  LinkStatusActive : TRUE\n  ConfigMethod : Manual\n",
    "route -n get default": "gateway: 10.0.0.1\ninterface: en1",
    "getifaddr en1": "10.0.0.10",
    "getoption en1 subnet_mask": "255.255.255.0",
    "ifconfig": "utun3: flags=...\n\tinet 100.107.112.118 --> 100.107.112.118",
    "ping": "3 packets transmitted, 3 packets received, 0.0% packet loss",
    "-getinfo": "IP address: 10.0.0.10\nSubnet mask: 255.255.255.0\nRouter: 10.0.0.1\n",
}

# After the flip, the node is on DHCP with a reachable gateway — a real heal.
_STATIC_DRIFT_AFTER: dict[str, str] = {
    **_STATIC_DRIFT_BEFORE,
    "getsummary en1": "  LinkStatusActive : TRUE\n  ConfigMethod : DHCP\n",
    "ping": "3 packets transmitted, 3 packets received, 0.0% packet loss",
}

# After the flip the gateway is STILL dead (heal did not take) — CLI must revert.
_STATIC_DRIFT_STAYS_BROKEN: dict[str, str] = {
    **_STATIC_DRIFT_BEFORE,
    "getsummary en1": "  LinkStatusActive : TRUE\n  ConfigMethod : DHCP\n",
    "ping": "3 packets transmitted, 0 packets received, 100.0% packet loss",
}

# A double-NAT overlap (10.x LAN inside Bell's 0/1 DMZ WAN) + dead gateway +
# spine up → risky DOUBLE_NAT_OVERLAP → alert-only, NEVER a mutation.
_OVERLAP_RISKY: dict[str, str] = {
    "listallhardwareports": "Hardware Port: Wi-Fi\nDevice: en1\nEthernet Address: d0:11:e5:1c:88:59",
    "getsummary en1": "  LinkStatusActive : TRUE\n  ConfigMethod : DHCP\n",
    "route -n get default": "gateway: 10.0.0.1\ninterface: en1",
    "getifaddr en1": "10.0.0.10",
    "getoption en1 subnet_mask": "255.255.255.0",
    "ifconfig": "utun3: flags=...\n\tinet 100.107.112.118 --> 100.107.112.118",
    "ping": "3 packets transmitted, 0 packets received, 100.0% packet loss",
    "-getinfo": "IP address: 10.0.0.10\nSubnet mask: 255.255.255.0\nRouter: 10.0.0.1\n",
}


def test_net_heal_dry_run_reports_static_drift_no_mutation() -> None:
    """Default `net heal` is a dry-run: prints STATIC_DRIFT + the would-do flip,
    and issues NO mutating command."""
    fake = _HealFakeRunner(_STATIC_DRIFT_BEFORE)
    with patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake):
        result = runner.invoke(app, ["net", "heal"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "STATIC_DRIFT" in out
    assert "flip_dhcp" in out
    # Dry-run must never mutate: no setdhcp / no ipconfig-set command was issued.
    assert not any(
        "-setdhcp" in " ".join(c) or "set en1 DHCP" in " ".join(c) for c in fake.calls
    )


def test_net_heal_dry_run_shows_spine_state() -> None:
    """The dry-run surfaces the never-strand spine state (tailnet / TB5)."""
    fake = _HealFakeRunner(_STATIC_DRIFT_BEFORE)
    with patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake):
        result = runner.invoke(app, ["net", "heal"])
    assert result.exit_code == 0, result.stdout
    # The tailnet spine (100.107.x present) must be reported as up.
    lower = result.stdout.lower()
    assert "tailnet" in lower or "spine" in lower


def test_net_heal_apply_non_root_prints_sudo_hint_no_mutation() -> None:
    """`--apply` from a non-root shell refuses to mutate and prints a sudo hint."""
    fake = _HealFakeRunner(_STATIC_DRIFT_BEFORE)
    with (
        patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake),
        patch("sanctum_cli.commands.net.os.getuid", return_value=501),
    ):
        result = runner.invoke(app, ["net", "heal", "--apply"])
    assert result.exit_code == 0, result.stdout
    assert "sudo" in result.stdout.lower()
    # Non-root: still no mutation.
    assert not any(
        "-setdhcp" in " ".join(c) or "set en1 DHCP" in " ".join(c) for c in fake.calls
    )


def test_net_heal_apply_heals_and_verifies() -> None:
    """`--apply` (as root) on a STATIC_DRIFT node that comes back healthy:
    fires the flip, re-probes, and prints ✓ from the REAL re-probe."""
    fake = _HealFakeRunner(_STATIC_DRIFT_BEFORE, _STATIC_DRIFT_AFTER)
    with (
        patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake),
        patch("sanctum_cli.commands.net.os.getuid", return_value=0),
    ):
        result = runner.invoke(app, ["net", "heal", "--apply"])
    assert result.exit_code == 0, result.stdout
    # The flip actually fired.
    assert any("-setdhcp" in " ".join(c) for c in fake.calls)
    assert "✓" in result.stdout or "healed" in result.stdout.lower()
    # No revert on a successful heal.
    assert not any("-setmanual" in " ".join(c) for c in fake.calls)


def test_net_heal_apply_stays_broken_reverts_and_alerts() -> None:
    """`--apply` where the re-probe STAYS broken must revert to the snapshot and
    stop+alert (honest-verify: no false ✓)."""
    fake = _HealFakeRunner(_STATIC_DRIFT_BEFORE, _STATIC_DRIFT_STAYS_BROKEN)
    with (
        patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake),
        patch("sanctum_cli.commands.net.os.getuid", return_value=0),
    ):
        result = runner.invoke(app, ["net", "heal", "--apply"])
    assert result.exit_code == 0, result.stdout
    # The flip fired, then the revert (setmanual with the saved values) fired.
    assert any("-setdhcp" in " ".join(c) for c in fake.calls)
    assert any("-setmanual" in " ".join(c) for c in fake.calls)
    lower = result.stdout.lower()
    assert "revert" in lower and ("✗" in result.stdout or "not healed" in lower or "stop" in lower)


def test_net_heal_apply_risky_alert_only_no_mutation() -> None:
    """A risky DOUBLE_NAT_OVERLAP node under `--apply` alerts + issues NO mutation
    (stays out of the NAT domain)."""
    fake = _HealFakeRunner(_OVERLAP_RISKY)
    with (
        patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake),
        patch("sanctum_cli.commands.net.os.getuid", return_value=0),
    ):
        result = runner.invoke(app, ["net", "heal", "--apply"])
    assert result.exit_code == 0, result.stdout
    assert "DOUBLE_NAT_OVERLAP" in result.stdout
    # Never touches the interface on a risky verdict.
    assert not any(
        "-setdhcp" in " ".join(c) or "set en1 DHCP" in " ".join(c) or "-setmanual" in " ".join(c)
        for c in fake.calls
    )


# ─── NET_HEAL_RESULT token — kill the "not healed" substring collision ─────
#
# Whole-branch review found a NO-LOOP defeat: the daemon wrapper detected success
# with `grep -q 'healed'`, but the CLI's FAILURE line ("✗ not healed — … reverting
# …") CONTAINS the substring "healed". So a reverted heal matched the success
# branch → the wrapper reset the attempts counter to 0 every cycle → the
# MAX_HEAL_ATTEMPTS cap never accrued → the daemon re-fired the failing heal every
# 120s forever (the toggle-storm the cap exists to prevent). These are the
# hostile-input regressions proving the collision is dead: the CLI now emits an
# unambiguous machine-readable token, and the wrapper anchors on the exact token.


def test_net_heal_apply_success_emits_healed_token() -> None:
    """A real, verified heal emits the machine-readable `NET_HEAL_RESULT=healed`
    token — derived from the SAME real re-probe as the human ✓ (honest-verify)."""
    fake = _HealFakeRunner(_STATIC_DRIFT_BEFORE, _STATIC_DRIFT_AFTER)
    with (
        patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake),
        patch("sanctum_cli.commands.net.os.getuid", return_value=0),
    ):
        result = runner.invoke(app, ["net", "heal", "--apply"])
    assert result.exit_code == 0, result.stdout
    assert heal.HEAL_RESULT_HEALED in result.stdout  # NET_HEAL_RESULT=healed
    # Only the success token — never the reverted/noop tokens on a real heal.
    assert heal.HEAL_RESULT_REVERTED not in result.stdout
    assert heal.HEAL_RESULT_NOOP not in result.stdout


def test_net_heal_apply_revert_emits_reverted_not_healed_token() -> None:
    """THE hostile-input regression: a fired-but-stayed-broken heal reverts and its
    output contains the human word "healed" (in "✗ not healed") — but MUST NOT
    contain the success token `NET_HEAL_RESULT=healed`. It emits `=reverted`."""
    fake = _HealFakeRunner(_STATIC_DRIFT_BEFORE, _STATIC_DRIFT_STAYS_BROKEN)
    with (
        patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake),
        patch("sanctum_cli.commands.net.os.getuid", return_value=0),
    ):
        result = runner.invoke(app, ["net", "heal", "--apply"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # The human failure prose (still present for operators) DOES contain "healed".
    assert "not healed" in out.lower()
    # …but the machine-readable success token MUST NOT be present — the collision.
    assert heal.HEAL_RESULT_HEALED not in out, (
        "revert path leaked the success token — the 'not healed' substring "
        "collision is back and the no-loop cap will never accrue"
    )
    # The correct token is emitted instead.
    assert heal.HEAL_RESULT_REVERTED in out  # NET_HEAL_RESULT=reverted


def test_net_heal_apply_risky_emits_noop_token() -> None:
    """A stop-and-alert (risky / non-mutating) `--apply` emits `NET_HEAL_RESULT=noop`
    and never the healed token — so the wrapper counts it as a no-heal cycle."""
    fake = _HealFakeRunner(_OVERLAP_RISKY)
    with (
        patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake),
        patch("sanctum_cli.commands.net.os.getuid", return_value=0),
    ):
        result = runner.invoke(app, ["net", "heal", "--apply"])
    assert result.exit_code == 0, result.stdout
    assert heal.HEAL_RESULT_NOOP in result.stdout  # NET_HEAL_RESULT=noop
    assert heal.HEAL_RESULT_HEALED not in result.stdout


def test_wrapper_success_detector_matches_healed_but_not_revert_output() -> None:
    """Cross-layer contract (Contracts at the Boundary): feed the REAL CLI outputs
    through the EXACT `grep` the shipped HEAL_WRAPPER uses. The grep pattern comes
    from production (heal.HEAL_RESULT_HEALED, embedded in HEAL_WRAPPER); the output
    comes from the real CLI — no shared assumption. The detector MUST match the
    healed output and MUST NOT match the reverted output (else the counter resets
    on every reverted heal and the no-loop cap never accrues)."""
    import subprocess

    # The wrapper's exact anchored pattern (single-source-of-truth token).
    pattern = heal.HEAL_RESULT_HEALED
    assert f"grep -q '{pattern}'" in heal.HEAL_WRAPPER, (
        "the shipped wrapper no longer greps the anchored token this test proves"
    )

    def cli_apply_output(fake: _HealFakeRunner) -> str:
        with (
            patch("sanctum_cli.commands.net._build_heal_runner", return_value=fake),
            patch("sanctum_cli.commands.net.os.getuid", return_value=0),
        ):
            return runner.invoke(app, ["net", "heal", "--apply"]).stdout

    def grep_matches(text: str) -> bool:
        # The real `grep -q '<token>'` the wrapper runs (RC 0 == matched).
        proc = subprocess.run(
            ["grep", "-q", pattern], input=text, text=True, check=False
        )
        return proc.returncode == 0

    healed_out = cli_apply_output(_HealFakeRunner(_STATIC_DRIFT_BEFORE, _STATIC_DRIFT_AFTER))
    reverted_out = cli_apply_output(
        _HealFakeRunner(_STATIC_DRIFT_BEFORE, _STATIC_DRIFT_STAYS_BROKEN)
    )
    # Success branch (resets ATTEMPTS to 0) fires ONLY on a real heal.
    assert grep_matches(healed_out) is True
    # Reverted heal falls through to the else branch → ATTEMPTS increments.
    assert grep_matches(reverted_out) is False


# ─── net heal --install (Task 5 — self-healing LaunchDaemon) ─────────
#
# The install path writes a wrapper (0755) + a LaunchDaemon plist into paths
# redirected to a temp dir, and stubs the one sudo step (launchctl bootstrap
# system) so nothing touches /Library/LaunchDaemons or shells out for real.


def test_net_heal_install_writes_wrapper_and_plist_as_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--install` (as root) writes the wrapper 0755 + a plist naming it, then
    best-effort bootstraps it into the system domain."""
    wrapper = tmp_path / "sanctum" / "net-heal.sh"
    plist = tmp_path / "LaunchDaemons" / "com.sanctum.net-heal.plist"
    err_log = tmp_path / "logs" / "net-heal.err"
    monkeypatch.setattr("sanctum_cli.net.heal.heal_wrapper_path", lambda: wrapper)
    monkeypatch.setattr("sanctum_cli.net.heal.heal_plist_path", lambda: plist)
    monkeypatch.setattr("sanctum_cli.net.heal.heal_err_path", lambda: err_log)

    calls: list[list[str]] = []

    def fake_launchctl(args: list[str], *, check: bool) -> tuple[bool, str]:
        calls.append(args)
        return (True, "")

    with (
        patch("sanctum_cli.commands.net._heal_launchctl", fake_launchctl),
        patch("sanctum_cli.commands.net.os.getuid", return_value=0),
    ):
        result = runner.invoke(app, ["net", "heal", "--install"])
    assert result.exit_code == 0, result.stdout

    # Real artifacts on disk.
    assert wrapper.exists()
    assert wrapper.read_text(encoding="utf-8").startswith("#!/bin/bash")
    assert oct(wrapper.stat().st_mode & 0o777) == "0o755"

    plist_text = plist.read_text(encoding="utf-8")
    assert str(wrapper) in plist_text
    assert "com.sanctum.net-heal" in plist_text
    assert str(err_log) in plist_text

    # A bootstrap into the SYSTEM domain was attempted (the one sudo step).
    assert any(a[:1] == ["bootstrap"] for a in calls)
    assert any("system" in " ".join(a) for a in calls)


def test_net_heal_install_non_root_prints_sudo_hint_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--install` from a non-root shell refuses to write the system daemon and
    prints the exact sudo command (never a silent partial install)."""
    wrapper = tmp_path / "sanctum" / "net-heal.sh"
    plist = tmp_path / "LaunchDaemons" / "com.sanctum.net-heal.plist"
    monkeypatch.setattr("sanctum_cli.net.heal.heal_wrapper_path", lambda: wrapper)
    monkeypatch.setattr("sanctum_cli.net.heal.heal_plist_path", lambda: plist)
    monkeypatch.setattr("sanctum_cli.net.heal.heal_err_path", lambda: tmp_path / "x.err")

    with (
        patch("sanctum_cli.commands.net.os.getuid", return_value=501),
    ):
        result = runner.invoke(app, ["net", "heal", "--install"])
    assert result.exit_code == 0, result.stdout
    assert "sudo" in result.stdout.lower()
    # Non-root: nothing written to the system paths.
    assert not wrapper.exists()
    assert not plist.exists()


# ─── net status (Task 3 — one-glance roll-up pane) ───────────────────
#
# The status handler gathers each subsystem behind a module-level probe seam
# (each wrapped so a raised error degrades that row to UNKNOWN — never a crash),
# then calls the pure `build_status_report` assembler and renders an apple-like
# pane. Tests patch the seams so NO live networksetup/ipconfig/route/ping/ssh/
# launchctl is ever touched. The seams return the same subsystem value objects
# the pure assembler consumes.


def _stable_identity_diag():
    from sanctum_cli.net.link import IdentityDiagnosis, IdentityProbe

    probe = IdentityProbe(
        iface="en1", ssid="Manoir", current_mac="d0:11:e5:1c:88:59",
        hardware_mac="d0:11:e5:1c:88:59", security="WPA3", associated=True,
        router_arp_verified=True, gateway_reachable=True,
    )
    return IdentityDiagnosis("IDENTITY_STABLE", "on hardware MAC", "ok", probe)


def _quarantined_identity_diag():
    from sanctum_cli.net.link import IdentityDiagnosis, IdentityProbe

    probe = IdentityProbe(
        iface="en1", ssid="Manoir", current_mac="32:a6:f4:de:54:cf",
        hardware_mac="d0:11:e5:1c:88:59", security="WPA3", associated=True,
        router_arp_verified=False, gateway_reachable=False,
    )
    return IdentityDiagnosis("IDENTITY_QUARANTINED", "rotating, gw dead", "pin it", probe)


def _healthy_posture_diag():
    from sanctum_cli.net.heal import HealAction, PostureDiagnosis

    posture = heal.NetPosture(
        iface="en1", config_method="DHCP", ip="192.168.2.20",
        subnet="255.255.255.0", gateway="192.168.2.1", gateway_reachable=True,
        associated=True, on_tailnet=True, tb5_up=True,
    )
    return PostureDiagnosis(
        "HEALTHY", "healthy", "", HealAction("none", safe=True, detail="no action"), posture
    )


def _single_nat_topology():
    from sanctum_cli.net.types import Nat, TopologyReport

    return TopologyReport(
        firewalla_present=True, firewalla_wan_mac="20:6d:31:51:67:82",
        firewalla_wan_mtu=1500, nat=Nat.SINGLE, gateway_ip="192.168.2.1",
        isp="bell", public_ip="1.2.3.4", applicable=False, reason="optimal",
    )


def _patch_all_green_status():
    """Patch every status probe seam to a healthy value. Returns the context managers."""
    from sanctum_cli.net.status import DaemonInfo, GuardianInfo, SpineInfo

    return [
        patch("sanctum_cli.commands.net._status_probe_posture", return_value=_healthy_posture_diag()),
        patch("sanctum_cli.commands.net._status_probe_spine", return_value=SpineInfo(on_tailnet=True, tb5_up=True)),
        patch("sanctum_cli.commands.net._status_probe_daemon", return_value=DaemonInfo(loaded=True, last_result="healed")),
        patch("sanctum_cli.commands.net._status_probe_identity", return_value=_stable_identity_diag()),
        patch("sanctum_cli.commands.net._status_probe_topology", return_value=_single_nat_topology()),
        patch("sanctum_cli.commands.net._status_probe_guardian", return_value=GuardianInfo(reachable=True, fresh=True, age_seconds=120)),
    ]


def test_net_status_all_green_renders_every_subsystem_and_verdict() -> None:
    from contextlib import ExitStack

    with ExitStack() as stack:
        for cm in _patch_all_green_status():
            stack.enter_context(cm)
        result = runner.invoke(app, ["net", "status"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # Every subsystem label appears in the pane.
    for label in ("Posture", "Spine", "Heal daemon", "Identity", "Topology", "Guardian"):
        assert label in out, f"{label} missing from pane:\n{out}"
    # The overall verdict is surfaced.
    assert "GREEN" in out


def test_net_status_heal_daemon_down_shows_degraded() -> None:
    from contextlib import ExitStack

    from sanctum_cli.net.status import DaemonInfo

    patches = _patch_all_green_status()
    # Override the daemon seam to a not-loaded daemon.
    patches[2] = patch(
        "sanctum_cli.commands.net._status_probe_daemon",
        return_value=DaemonInfo(loaded=False, last_result=None),
    )
    with ExitStack() as stack:
        for cm in patches:
            stack.enter_context(cm)
        result = runner.invoke(app, ["net", "status"])
    assert result.exit_code == 0, result.stdout
    assert "DEGRADED" in result.stdout


def test_net_status_identity_quarantined_shows_degraded() -> None:
    from contextlib import ExitStack

    patches = _patch_all_green_status()
    patches[3] = patch(
        "sanctum_cli.commands.net._status_probe_identity",
        return_value=_quarantined_identity_diag(),
    )
    with ExitStack() as stack:
        for cm in patches:
            stack.enter_context(cm)
        result = runner.invoke(app, ["net", "status"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "DEGRADED" in out
    assert "IDENTITY_QUARANTINED" in out


def test_net_status_guardian_unreachable_is_unknown_not_crash() -> None:
    from contextlib import ExitStack

    from sanctum_cli.net.status import GuardianInfo

    patches = _patch_all_green_status()
    patches[5] = patch(
        "sanctum_cli.commands.net._status_probe_guardian",
        return_value=GuardianInfo(reachable=False, fresh=None, age_seconds=None),
    )
    with ExitStack() as stack:
        for cm in patches:
            stack.enter_context(cm)
        result = runner.invoke(app, ["net", "status"])
    assert result.exit_code == 0, result.stdout
    # Best-effort guardian: unknown does NOT drag the node to DEGRADED.
    assert "GREEN" in result.stdout


def test_net_status_raised_probe_error_degrades_to_unknown_not_crash() -> None:
    """A probe seam that RAISES must be caught → that row renders UNKNOWN, and the
    pane still renders (never a crash / non-zero exit)."""
    from contextlib import ExitStack

    def _boom() -> object:
        raise RuntimeError("probe blew up")

    patches = _patch_all_green_status()
    # Make the posture probe raise. The handler must swallow it → UNKNOWN row.
    patches[0] = patch("sanctum_cli.commands.net._status_probe_posture", side_effect=_boom)
    with ExitStack() as stack:
        for cm in patches:
            stack.enter_context(cm)
        result = runner.invoke(app, ["net", "status"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # The pane still rendered every row (including the crashed one).
    assert "Posture" in out
    # The crashed subsystem shows UNKNOWN and did not take the whole pane down.
    assert "UNKNOWN" in out.upper()
    # The healthy remainder still rendered.
    assert "Spine" in out and "Guardian" in out


def test_net_status_all_probes_fail_renders_all_unknown() -> None:
    """Every probe raising still yields a full pane of UNKNOWN rows (fail-closed per
    row, never a crash)."""
    from contextlib import ExitStack

    def _boom() -> object:
        raise RuntimeError("no network")

    seams = (
        "_status_probe_posture", "_status_probe_spine", "_status_probe_daemon",
        "_status_probe_identity", "_status_probe_topology", "_status_probe_guardian",
    )
    with ExitStack() as stack:
        for seam in seams:
            stack.enter_context(patch(f"sanctum_cli.commands.net.{seam}", side_effect=_boom))
        result = runner.invoke(app, ["net", "status"])
    assert result.exit_code == 0, result.stdout
    for label in ("Posture", "Spine", "Heal daemon", "Identity", "Topology", "Guardian"):
        assert label in result.stdout
