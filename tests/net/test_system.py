from __future__ import annotations

import subprocess
from unittest.mock import patch

from sanctum_cli.net import system


def test_real_runner_maps_tags_to_commands() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="out\n", stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        out = system.real_runner(("traceroute",))
    assert out == "out\n"
    assert any("traceroute" in part for part in calls[0])


def test_real_runner_returns_empty_on_failure() -> None:
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=boom):
        assert system.real_runner(("traceroute",)) == ""


def test_real_runner_unknown_tag_returns_empty() -> None:
    assert system.real_runner(("fw_wan_ip",)) == ""


def test_make_real_runner_serves_fw_tags_and_delegates() -> None:
    delegated: list[tuple[str, ...]] = []

    def fake_real_runner(tag: tuple[str, ...]) -> str:
        delegated.append(tag)
        return "traceroute-out"

    with (
        patch(
            "sanctum_cli.net.system.firewalla_wan_via_ssh",
            return_value=("9.9.9.9", "aa:bb:cc:dd:ee:ff"),
        ),
        patch("sanctum_cli.net.system.real_runner", side_effect=fake_real_runner),
    ):
        runner = system.make_real_runner(fw_gateway="10.0.0.1", fw_key="/tmp/k")
        assert runner(("fw_wan_ip",)) == "9.9.9.9"
        assert runner(("fw_wan_mac",)) == "aa:bb:cc:dd:ee:ff"
        assert runner(("traceroute",)) == "traceroute-out"
    assert delegated == [("traceroute",)]


def test_make_real_runner_caches_single_probe() -> None:
    with patch(
        "sanctum_cli.net.system.firewalla_wan_via_ssh",
        return_value=("9.9.9.9", "aa:bb:cc:dd:ee:ff"),
    ) as probe:
        runner = system.make_real_runner(fw_gateway="10.0.0.1", fw_key="/tmp/k")
        runner(("fw_wan_ip",))
        runner(("fw_wan_mac",))
    assert probe.call_count == 1


def test_make_real_runner_without_gateway_returns_empty_fw() -> None:
    with patch("sanctum_cli.net.system.firewalla_wan_via_ssh") as probe:
        runner = system.make_real_runner(fw_gateway=None, fw_key="/tmp/k")
        assert runner(("fw_wan_ip",)) == ""
        assert runner(("fw_wan_mac",)) == ""
    probe.assert_not_called()


def test_firewalla_wan_via_ssh_parses_mac_and_ip() -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="aa:bb:cc:dd:ee:ff\n9.9.9.9\n", stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        assert system.firewalla_wan_via_ssh("10.0.0.1", "/tmp/k") == (
            "9.9.9.9",
            "aa:bb:cc:dd:ee:ff",
        )


def test_firewalla_wan_via_ssh_returns_empty_on_timeout() -> None:
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 12)

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=boom):
        assert system.firewalla_wan_via_ssh("10.0.0.1", "/tmp/k") == ("", "")


def test_firewalla_wan_via_ssh_never_raises_on_decode_error() -> None:
    def boom(*a, **k):  # simulate a non-decodable / ValueError path
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=boom):
        assert system.firewalla_wan_via_ssh("10.0.0.1", "/tmp/key") == ("", "")


# ─── firewalla_ports_and_wan_via_ssh (impure, one round-trip) ────────

# The combined remote command frames its two answers with sentinel lines so one
# SSH round-trip returns BOTH the port rows and the WAN kind. The '===PORTS==='
# block carries 'name<TAB>mbps' rows; '===WAN===' carries the wan-kind token.
_FW_COMBINED_STDOUT = (
    "===PORTS===\n"
    "eth0\t1000\n"
    "eth3\t2500\n"
    "===WAN===\n"
    "pppoe\n"
)


def test_firewalla_ports_and_wan_via_ssh_parses_both() -> None:
    def fake_run(cmd, **kwargs):
        # one SSH invocation, one combined blob back
        return subprocess.CompletedProcess(cmd, 0, stdout=_FW_COMBINED_STDOUT, stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run) as run:
        ports, wan = system.firewalla_ports_and_wan_via_ssh("10.0.0.1", "/tmp/k")
    assert ports == [("eth0", 1000), ("eth3", 2500)]
    assert wan == "pppoe"
    assert run.call_count == 1  # single round-trip


def test_firewalla_ports_and_wan_via_ssh_uses_batchmode_key_only() -> None:
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_FW_COMBINED_STDOUT, stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        system.firewalla_ports_and_wan_via_ssh("10.0.0.1", "/tmp/k", user="pi")
    argv = captured[0]
    assert argv[0] == "ssh"
    assert "-i" in argv and "/tmp/k" in argv
    assert "BatchMode=yes" in argv
    assert "PreferredAuthentications=publickey" in argv
    assert "pi@10.0.0.1" in argv


def test_firewalla_ports_and_wan_via_ssh_dhcp_no_ports() -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="===PORTS===\n===WAN===\ndhcp\n", stderr=""
        )

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        ports, wan = system.firewalla_ports_and_wan_via_ssh("10.0.0.1", "/tmp/k")
    assert ports == []
    assert wan == "dhcp"


def test_firewalla_ports_and_wan_via_ssh_empty_on_timeout() -> None:
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 12)

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=boom):
        assert system.firewalla_ports_and_wan_via_ssh("10.0.0.1", "/tmp/k") == ([], "")


def test_firewalla_ports_and_wan_via_ssh_never_raises_on_decode_error() -> None:
    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=boom):
        assert system.firewalla_ports_and_wan_via_ssh("10.0.0.1", "/tmp/k") == ([], "")


# ─── speedtest probe helpers ─────────────────────────────────────────


def test_parse_link_speed_ifconfig_media() -> None:
    out = "en7: flags=8863 mtu 1500\n\tmedia: autoselect (10Gbase-T <full-duplex>)\n\tstatus: active\n"
    assert system.parse_link_speed_mbps(out) == 10000


def test_parse_link_speed_ifconfig_2_5g() -> None:
    out = "\tmedia: autoselect (2500Base-T <full-duplex>)\n"
    assert system.parse_link_speed_mbps(out) == 2500


def test_parse_link_speed_baset_no_g() -> None:
    out = "\tmedia: autoselect (1000baseT <full-duplex>)\n"
    assert system.parse_link_speed_mbps(out) == 1000


def test_parse_link_speed_ethtool_linux() -> None:
    out = "Settings for eth0:\n\tSpeed: 2500Mb/s\n\tDuplex: Full\n"
    assert system.parse_link_speed_mbps(out) == 2500


def test_parse_link_speed_unknown_returns_none() -> None:
    assert system.parse_link_speed_mbps("status: inactive\n") is None
    assert system.parse_link_speed_mbps("") is None


def test_parse_default_iface_route_output() -> None:
    out = "   route to: default\ndestination: default\n  interface: en7\n  gateway: 10.0.0.1\n"
    assert system.parse_default_iface(out) == "en7"


def test_parse_default_iface_missing_returns_none() -> None:
    assert system.parse_default_iface("gateway: 10.0.0.1\n") is None


def test_iface_is_wifi_from_airport_listing() -> None:
    listing = (
        "Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: aa:bb\n\n"
        "Hardware Port: Ethernet\nDevice: en7\nEthernet Address: cc:dd\n"
    )
    assert system.iface_is_wifi("en0", listing) is True
    assert system.iface_is_wifi("en7", listing) is False


def test_iface_is_wifi_unknown_iface_returns_none() -> None:
    listing = "Hardware Port: Wi-Fi\nDevice: en0\n"
    assert system.iface_is_wifi("en9", listing) is None


# ─── parse_fw_ports (pure) ───────────────────────────────────────────


def test_parse_fw_ports_from_sys_class_net_speed_lines() -> None:
    # The remote emits one "name<TAB>mbps" row per link-up WAN/LAN port
    # (read from /sys/class/net/<dev>/speed). A -1 / 0 / unknown speed is dropped.
    blob = "eth0\t1000\neth3\t2500\n"
    assert system.parse_fw_ports(blob) == [("eth0", 1000), ("eth3", 2500)]


def test_parse_fw_ports_drops_unknown_and_malformed() -> None:
    # -1 (link down, per the kernel), a blank speed, and a junk row are all dropped.
    blob = "eth0\t1000\neth1\t-1\neth2\t\ngarbage-line\neth3\t2500\n"
    assert system.parse_fw_ports(blob) == [("eth0", 1000), ("eth3", 2500)]


def test_parse_fw_ports_empty_returns_empty_list() -> None:
    assert system.parse_fw_ports("") == []
    assert system.parse_fw_ports("\n\n") == []


def test_parse_fw_ports_tolerates_ethtool_speed_rows() -> None:
    # A remote that shells `ethtool <dev>` may emit "name<TAB>2500Mb/s" style;
    # the parser reads the leading integer of the Mbps field either way.
    blob = "eth0\t1000Mb/s\neth3\t2500Mb/s\n"
    assert system.parse_fw_ports(blob) == [("eth0", 1000), ("eth3", 2500)]


# ─── parse_wan_kind (pure) ───────────────────────────────────────────


def test_parse_wan_kind_pppoe_from_ppp_iface() -> None:
    # `ip link show type ppp` output naming a pppN iface -> pppoe.
    out = "3: ppp0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1492 qdisc fq_codel\n"
    assert system.parse_wan_kind(out) == "pppoe"


def test_parse_wan_kind_pppoe_bare_token() -> None:
    assert system.parse_wan_kind("pppoe\n") == "pppoe"


def test_parse_wan_kind_dhcp() -> None:
    assert system.parse_wan_kind("dhcp\n") == "dhcp"


def test_parse_wan_kind_static() -> None:
    assert system.parse_wan_kind("static\n") == "static"


def test_parse_wan_kind_empty_or_unknown_returns_empty() -> None:
    assert system.parse_wan_kind("") == ""
    assert system.parse_wan_kind("   \n") == ""
    assert system.parse_wan_kind("something-else\n") == ""


def test_parse_speedtest_cli_json() -> None:
    blob = '{"download": {"bandwidth": 987500000}, "upload": {"bandwidth": 123000000}}'
    # bandwidth is bytes/sec -> *8/1e6 = Mbps
    assert system.parse_speedtest_cli_mbps(blob) == 7900.0


def test_parse_speedtest_cli_json_garbage_returns_none() -> None:
    assert system.parse_speedtest_cli_mbps("not json") is None
    assert system.parse_speedtest_cli_mbps("{}") is None
