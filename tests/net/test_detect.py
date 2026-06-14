from __future__ import annotations

from sanctum_cli.net import detect
from sanctum_cli.net.types import Nat


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
