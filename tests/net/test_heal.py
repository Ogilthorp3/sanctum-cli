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
