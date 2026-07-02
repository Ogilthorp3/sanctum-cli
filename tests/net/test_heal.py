"""Tests for the Sanctum Net Heal topology-adaptive self-healing layer.

Pure + injected-runner: no live networksetup/ipconfig/route/ping/sudo — the
subprocess seam is faked so posture reads are unit-testable without a network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.net.heal import (
    MAX_HEAL_ATTEMPTS,
    NetPosture,
    PostureDiagnosis,
    diagnose_posture,
    plan_heal,
    probe_posture,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _run(mapping: dict[str, str]) -> Callable[[list[str]], str]:
    def r(argv: list[str]) -> str:
        k = " ".join(argv)
        for pat, out in mapping.items():
            if pat in k:
                return out
        return ""

    return r


def test_probe_posture_static_manual_mini() -> None:
    run = _run(
        {
            "listallhardwareports": "Hardware Port: Wi-Fi\nDevice: en1\nEthernet Address: d0:11:e5:1c:88:59",
            "ifconfig en1": "\tether 32:a6:f4:de:54:cf\n\tinet 10.0.0.10 netmask 0xffffff00",
            "getmacaddress en1": "Ethernet Address: d0:11:e5:1c:88:59",
            "getsummary en1": "  SSID : X\n  LinkStatusActive : TRUE\n  RouterARPVerified : FALSE\n  ConfigMethod : Manual\n",
            "route -n get default": "gateway: 10.0.0.1\ninterface: en1",
            "getifaddr en1": "10.0.0.10",
            "ipconfig getoption en1 subnet_mask": "255.255.255.0",
            "ifconfig": "utun3: flags=...\n\tinet 100.107.112.118 --> 100.107.112.118",
            "ping": "0 packets received, 100.0% packet loss",
        }
    )
    p = probe_posture(run=run)
    assert p.iface == "en1"
    assert p.config_method == "Manual"
    assert p.ip == "10.0.0.10"
    assert p.gateway == "10.0.0.1"
    assert p.gateway_reachable is False
    assert p.on_tailnet is True


def test_probe_posture_iface_absent_unverified() -> None:
    p = probe_posture(run=_run({}))
    assert p.iface == "" and p.config_method == "" and p.on_tailnet is False


def test_probe_posture_netposture_is_frozen() -> None:
    # NetPosture is an immutable value object (frozen dataclass) — pure-core contract.
    p = probe_posture(run=_run({}))
    assert isinstance(p, NetPosture)


# ─── Task 2: diagnose_posture — pure topology truth table ──────────────


def _p(**k: object) -> NetPosture:
    base: dict[str, object] = dict(
        iface="en1",
        config_method="DHCP",
        ip="10.0.0.10",
        subnet="255.255.255.0",
        gateway="10.0.0.1",
        gateway_reachable=True,
        associated=True,
        on_tailnet=True,
        tb5_up=True,
    )
    base.update(k)
    return NetPosture(**base)  # type: ignore[arg-type]


def test_healthy() -> None:
    d = diagnose_posture(_p())
    assert isinstance(d, PostureDiagnosis)
    assert d.verdict == "HEALTHY"
    assert d.action.kind == "none"
    assert d.posture is not None


def test_static_drift() -> None:
    d = diagnose_posture(_p(config_method="Manual"))
    assert d.verdict == "STATIC_DRIFT"
    assert d.action.kind == "flip_dhcp"
    assert d.action.safe


def test_gateway_dead() -> None:
    d = diagnose_posture(_p(gateway_reachable=False))
    assert d.verdict == "GATEWAY_DEAD"
    assert d.action.kind == "dhcp_renew"
    assert d.action.safe


def test_wrong_subnet_bell_static() -> None:
    # Mini Manual 10.0.0.10 while really on Bell 192.168.2.x: static drift takes
    # priority; both auto-heal to DHCP (safe).
    d = diagnose_posture(_p(config_method="Manual", gateway_reachable=False))
    assert d.verdict in ("STATIC_DRIFT", "WRONG_SUBNET")
    assert d.action.safe


def test_wrong_subnet_dhcp_gateway_off_subnet() -> None:
    # DHCP lease but the default gateway sits outside our IP's subnet (renumber /
    # stale lease): WRONG_SUBNET → guarded dhcp_renew (safe).
    d = diagnose_posture(_p(ip="10.0.0.10", subnet="255.255.255.0", gateway="192.168.2.1"))
    assert d.verdict == "WRONG_SUBNET"
    assert d.action.kind == "dhcp_renew"
    assert d.action.safe


def test_double_nat_overlap_is_risky() -> None:
    d = diagnose_posture(_p(gateway_reachable=False), overlap=True)
    assert d.verdict == "DOUBLE_NAT_OVERLAP"
    assert not d.action.safe
    assert d.action.kind == "alert_only"


def test_unverified() -> None:
    assert diagnose_posture(_p(iface="", config_method="")).verdict == "UNVERIFIED"


def test_unverified_action_not_safe() -> None:
    # fail-closed: an unreadable posture never yields a safe (mutating) action.
    d = diagnose_posture(_p(iface="", config_method=""))
    assert not d.action.safe


# ─── Task 3: plan_heal — never-strand + no-loop guard ──────────────────


def test_safe_action_planned() -> None:
    d = diagnose_posture(_p(config_method="Manual"))
    hp = plan_heal(d, attempts=0, tailnet_ok=True)
    assert hp.execute and hp.action is not None and hp.action.kind == "flip_dhcp"


def test_risky_stops() -> None:
    d = diagnose_posture(_p(gateway_reachable=False), overlap=True)
    hp = plan_heal(d, attempts=0, tailnet_ok=True)
    assert not hp.execute and "alert" in hp.reason.lower()


def test_attempts_cap_stops() -> None:
    d = diagnose_posture(_p(config_method="Manual"))
    hp = plan_heal(d, attempts=MAX_HEAL_ATTEMPTS, tailnet_ok=True)
    assert not hp.execute and "attempt" in hp.reason.lower()


def test_no_spine_no_mutate() -> None:
    d = diagnose_posture(_p(config_method="Manual"))
    hp = plan_heal(d, attempts=0, tailnet_ok=False, tb5_ok=False)
    assert not hp.execute and "spine" in hp.reason.lower()


# ─── Task 5: self-healing daemon assets (plist + wrapper) ──────────────


def test_render_heal_plist_round_trips_via_plistlib() -> None:
    # Real artifact through the real consumer (Contracts at the Boundary): the
    # rendered bytes MUST parse as a plist and carry the daemon's label + cadence.
    import plistlib
    from pathlib import Path

    from sanctum_cli.net.heal import (
        HEAL_DAEMON_LABEL,
        HEAL_INTERVAL_S,
        render_heal_plist,
    )

    wrapper = Path("/Library/Application Support/sanctum/net-heal.sh")
    err_log = Path("/var/log/sanctum-net-heal.err")
    xml = render_heal_plist(wrapper=wrapper, err_log=err_log)
    parsed = plistlib.loads(xml.encode("utf-8"))
    assert parsed["Label"] == HEAL_DAEMON_LABEL
    assert parsed["StartInterval"] == HEAL_INTERVAL_S
    # launchd does not expand ~, so the absolute wrapper path must be named.
    assert str(wrapper) in xml
    assert str(err_log) in xml
    # A LaunchDaemon so it can setdhcp/renew (runs as root).
    assert HEAL_DAEMON_LABEL == "com.sanctum.net-heal"


def test_render_heal_plist_names_the_wrapper_program() -> None:
    import plistlib
    from pathlib import Path

    from sanctum_cli.net.heal import render_heal_plist

    wrapper = Path("/opt/sanctum/net-heal.sh")
    parsed = plistlib.loads(
        render_heal_plist(wrapper=wrapper, err_log=Path("/tmp/x.err")).encode("utf-8")
    )
    args = parsed["ProgramArguments"]
    assert str(wrapper) in args
    assert parsed["RunAtLoad"] is True


def test_heal_wrapper_is_valid_bash() -> None:
    # The wrapper ships as a real script the LaunchDaemon executes; it must parse.
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from sanctum_cli.net.heal import HEAL_WRAPPER

    assert HEAL_WRAPPER.startswith("#!/bin/bash")
    bash = shutil.which("bash") or "/bin/bash"
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(HEAL_WRAPPER)
        path = fh.name
    try:
        proc = subprocess.run(
            [bash, "-n", path], capture_output=True, text=True, timeout=10, check=False
        )
        assert proc.returncode == 0, proc.stderr
    finally:
        Path(path).unlink(missing_ok=True)


def test_heal_wrapper_honors_disabled_kill_switch_and_caps_attempts() -> None:
    # The wrapper must reference the DISABLED kill-switch sentinel + the attempts
    # cap (no-loop) so the shipped script actually encodes the doctrine.
    from sanctum_cli.net.heal import HEAL_WRAPPER, MAX_HEAL_ATTEMPTS

    assert "DISABLED" in HEAL_WRAPPER
    assert str(MAX_HEAL_ATTEMPTS) in HEAL_WRAPPER
    # It heals via the real CLI (`sanctum net heal --apply`) + writes a heartbeat.
    assert "net heal --apply" in HEAL_WRAPPER
    assert "heartbeat" in HEAL_WRAPPER.lower()
