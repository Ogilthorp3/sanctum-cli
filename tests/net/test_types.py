from __future__ import annotations

import dataclasses

from sanctum_cli.net.types import Baseline, Nat, Playbook, TopologyReport, Verdict


def test_enums_have_expected_values() -> None:
    assert Nat.SINGLE.value == "single"
    assert Nat.DOUBLE.value == "double"
    assert Nat.CGNAT.value == "cgnat"
    assert Nat.UNKNOWN.value == "unknown"
    assert {v.value for v in Verdict} == {
        "verified",
        "not_yet",
        "apipa_rollback",
        "inconclusive",
    }


def test_topology_report_is_frozen() -> None:
    r = TopologyReport(
        firewalla_present=True,
        firewalla_wan_mac="20:6d:31:51:67:82",
        firewalla_wan_mtu=1500,
        nat=Nat.DOUBLE,
        gateway_ip="192.168.2.1",
        isp="bell",
        public_ip="70.0.0.1",
        applicable=True,
        reason="double-NAT behind Bell gateway",
    )
    assert r.isp == "bell"
    try:
        r.isp = "rogers"  # type: ignore[misc]
        raise AssertionError("expected frozen dataclass")
    except dataclasses.FrozenInstanceError:
        pass


def test_playbook_and_baseline_construct() -> None:
    pb = Playbook(
        id="generic",
        display_name="Generic router",
        achieves="single_nat",
        gateway_ips=(),
        title_contains=(),
        admin_url_template="http://{gateway_ip}",
        steps=("Find the DMZ / exposed-host setting.",),
        gotchas=(),
        ordering=(),
        rollback=("Disable the DMZ / exposed-host setting.",),
    )
    assert pb.achieves == "single_nat"
    b = Baseline(wan_ip="192.168.2.10", gateway_ip="192.168.2.1", public_ip="70.0.0.1", mtu=1500)
    assert b.wan_ip == "192.168.2.10"
