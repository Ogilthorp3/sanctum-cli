from __future__ import annotations

from sanctum_cli.net import detect
from sanctum_cli.net.types import Nat, TopologyReport
from tests.net import fixtures as fx


def test_parse_hop2_picks_second_hop_ip() -> None:
    out = (
        "traceroute to 1.1.1.1, 2 hops max\n"
        " 1  10.0.0.1  1.1 ms  0.9 ms  0.9 ms\n"
        " 2  192.168.2.1  2.2 ms  2.0 ms  2.1 ms\n"
    )
    assert detect.parse_hop2(out) == "192.168.2.1"


def test_parse_hop2_handles_stars() -> None:
    out = "traceroute to 1.1.1.1\n 1  10.0.0.1  1 ms\n 2  * * *\n"
    assert detect.parse_hop2(out) is None


def test_classify_nat_double_when_hop2_private() -> None:
    assert detect.classify_nat(hop2="192.168.2.1", wan_ip="192.168.2.10") is Nat.DOUBLE


def test_classify_nat_single_when_hop2_public() -> None:
    assert detect.classify_nat(hop2="70.53.0.1", wan_ip="70.53.0.9") is Nat.SINGLE


def test_classify_nat_cgnat_when_wan_in_carrier_range() -> None:
    assert detect.classify_nat(hop2="100.64.0.1", wan_ip="100.96.0.5") is Nat.CGNAT


def test_classify_nat_unknown_when_no_signal() -> None:
    assert detect.classify_nat(hop2=None, wan_ip=None) is Nat.UNKNOWN


def test_is_apipa() -> None:
    assert detect.is_apipa("169.254.10.4") is True
    assert detect.is_apipa("10.0.0.1") is False
    assert detect.is_apipa(None) is False


def test_parse_default_gateway() -> None:
    out = (
        "   route to: default\ndestination: default\n"
        "       gateway: 192.168.2.1\n     interface: en1\n"
    )
    assert detect.parse_default_gateway(out) == "192.168.2.1"


def test_parse_mtu_from_ifconfig() -> None:
    out = "en1: flags=8863<UP> mtu 1500\n\tinet 192.168.2.10 netmask 0xffffff00\n"
    assert detect.parse_mtu(out) == 1500


def test_parse_mtu_for_specific_iface() -> None:
    out = (
        "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
        "\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,TSO4,TSO6>\n"
        "en1: flags=8863<UP> mtu 1500\n"
        "\tinet 192.168.2.10 netmask 0xffffff00\n"
    )
    # Global matches lo0 first -> 16384
    assert detect.parse_mtu(out) == 16384
    # Specific interface matches en1 -> 1500
    assert detect.parse_mtu(out, "en1") == 1500
    # Specific interface that doesn't exist -> None
    assert detect.parse_mtu(out, "en9") is None


def test_detect_double_nat_bell() -> None:
    rep = detect.detect(
        runner=fx.FakeRunner(fx.DOUBLE_NAT),
        http=fx.fake_http(200, "Bell Giga Hub"),
        firewalla_present=True,
    )
    assert isinstance(rep, TopologyReport)
    assert rep.nat is Nat.DOUBLE
    assert rep.firewalla_present is True
    assert rep.firewalla_wan_mac == "20:6d:31:51:67:82"
    assert rep.gateway_ip == "192.168.2.1"
    assert rep.isp == "bell"
    assert rep.applicable is True
    assert rep.wan_ip == "192.168.2.10"


def test_detect_single_nat_is_not_applicable() -> None:
    rep = detect.detect(
        runner=fx.FakeRunner(fx.SINGLE_NAT), http=fx.fake_http(200, "Bell"), firewalla_present=True
    )
    assert rep.nat is Nat.SINGLE
    assert rep.applicable is False
    assert "already" in rep.reason.lower()


def test_detect_no_firewalla_is_not_applicable() -> None:
    rep = detect.detect(
        runner=fx.FakeRunner(fx.NO_FIREWALLA), http=fx.fake_http(200, ""), firewalla_present=False
    )
    assert rep.firewalla_present is False
    assert rep.applicable is False
    assert "firewalla" in rep.reason.lower()


def test_detect_cgnat_is_not_applicable_with_reason() -> None:
    rep = detect.detect(
        runner=fx.FakeRunner(fx.CGNAT), http=fx.fake_http(200, ""), firewalla_present=True
    )
    assert rep.nat is Nat.CGNAT
    assert rep.applicable is False
    assert "cgnat" in rep.reason.lower() or "carrier" in rep.reason.lower()


def test_detect_apipa_double_still_applicable() -> None:
    rep = detect.detect(
        runner=fx.FakeRunner(fx.APIPA), http=fx.fake_http(200, "Bell"), firewalla_present=True
    )
    assert rep.nat is Nat.DOUBLE
    assert rep.applicable is True


# ── Bell Advanced-DMZ /1 overlap check ──────────────────────────────────────


def test_lan_conflicts_with_bell_dmz_10x_true() -> None:
    # 10.x LAN overlaps Bell's 0.0.0.0/1 Advanced-DMZ WAN → conflict.
    assert detect.lan_conflicts_with_bell_dmz("10.0.0.0/24") is True


def test_lan_conflicts_with_bell_dmz_192_false() -> None:
    assert detect.lan_conflicts_with_bell_dmz("192.168.50.0/24") is False


def test_lan_conflicts_with_bell_dmz_172_false() -> None:
    assert detect.lan_conflicts_with_bell_dmz("172.16.0.0/24") is False


def test_lan_conflicts_with_bell_dmz_127_true() -> None:
    # 127.x is the last block inside 0.0.0.0/1.
    assert detect.lan_conflicts_with_bell_dmz("127.0.0.0/8") is True


def test_lan_conflicts_with_bell_dmz_128_boundary_false() -> None:
    # 128.0.0.0/1 is the safe half — the boundary is exclusive.
    assert detect.lan_conflicts_with_bell_dmz("128.0.0.0/24") is False


def test_lan_conflicts_with_bell_dmz_bad_input_false() -> None:
    assert detect.lan_conflicts_with_bell_dmz("not-a-cidr") is False
    assert detect.lan_conflicts_with_bell_dmz("") is False
    assert detect.lan_conflicts_with_bell_dmz("999.0.0.0/8") is False


def test_lan_conflicts_with_bell_dmz_bare_ip_treated_as_host() -> None:
    # ipaddress accepts a bare address as a /32; 10.x → conflict, 200.x → safe.
    assert detect.lan_conflicts_with_bell_dmz("10.0.0.0") is True
    assert detect.lan_conflicts_with_bell_dmz("200.0.0.1") is False


def test_lan_conflicts_with_bell_dmz_wide_lan_straddling_boundary_true() -> None:
    # A LAN that even partially overlaps 0.0.0.0/1 conflicts.
    assert detect.lan_conflicts_with_bell_dmz("0.0.0.0/0") is True
