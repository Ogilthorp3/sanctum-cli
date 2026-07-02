"""Tests for the Sanctum Net Heal topology-adaptive self-healing layer.

Pure + injected-runner: no live networksetup/ipconfig/route/ping/sudo — the
subprocess seam is faked so posture reads are unit-testable without a network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.net.heal import NetPosture, probe_posture

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
