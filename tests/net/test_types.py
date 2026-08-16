from __future__ import annotations

import dataclasses

from sanctum_cli.net.types import (
    Baseline,
    Nat,
    Playbook,
    SpeedReport,
    TopologyReport,
    Verdict,
)


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


def test_playbook_new_optional_fields_default() -> None:
    # New fields are optional; existing-style construction must still work.
    pb = Playbook(
        id="generic",
        display_name="Generic router",
        achieves="single_nat",
        gateway_ips=(),
        title_contains=(),
        admin_url_template="http://{gateway_ip}",
        steps=("Find the DMZ.",),
        gotchas=(),
        ordering=(),
        rollback=("Disable the DMZ.",),
    )
    assert pb.prechecks == ()
    assert pb.mtu is None
    assert pb.alt_playbook is None


def test_playbook_new_optional_fields_settable() -> None:
    pb = Playbook(
        id="example",
        display_name="Example",
        achieves="single_nat",
        gateway_ips=(),
        title_contains=(),
        admin_url_template="http://{gateway_ip}",
        steps=("Step.",),
        gotchas=(),
        ordering=(),
        rollback=("Undo.",),
        prechecks=("Check the LAN.",),
        mtu=1492,
        alt_playbook="example-alt",
    )
    assert pb.prechecks == ("Check the LAN.",)
    assert pb.mtu == 1492
    assert pb.alt_playbook == "example-alt"


def test_speed_report_defaults_and_shape() -> None:
    r = SpeedReport(
        multi_gbps=7.9,
        single_gbps=1.8,
        ceiling_gbps=10.0,
        on_wifi=False,
        hops=(("router port", 10000), ("switch", 1000)),
        bottleneck="switch (1.0 Gbps link)",
        verdict="Your single-stream number was the artifact.",
        advice=("Single/double-NAT does not change throughput.",),
    )
    assert r.multi_gbps == 7.9
    assert r.test_inconclusive is False
    assert r.hops[1] == ("switch", 1000)
    try:
        r.verdict = "changed"  # type: ignore[misc]
        raise AssertionError("expected frozen dataclass")
    except dataclasses.FrozenInstanceError:
        pass


def test_speed_report_all_unknown() -> None:
    r = SpeedReport(
        multi_gbps=None,
        single_gbps=None,
        ceiling_gbps=None,
        on_wifi=None,
        hops=(),
        bottleneck="unknown",
        verdict="audit only",
        advice=(),
        test_inconclusive=True,
    )
    assert r.multi_gbps is None
    assert r.test_inconclusive is True
