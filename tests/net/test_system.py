from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

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


# ─── mutating Firewalla/firerouter tags (the apply path) ─────────────────────
#
# The four mutating tags the single-NAT DMZ orchestrator fires
# (sanctum_cli.devices.intents._RUNNER_*) MUST resolve to real Firewalla SSH
# commands over the fw key — NOT a silent "". The argv asserted here is derived
# from the REAL contract: the existing firewalla_wan_via_ssh SSH-options shape
# (BatchMode/publickey/ConnectTimeout) + the firerouter commands the armor kit's
# watchdog actually issues (sanctum-singlenat-armor/bin/singlenat-watchdog.sh:
# `sudo dhclient -r $WAN; sudo dhclient $WAN`, the WAN-dev derivation from
# `ip route show default`, `sudo bash post_main.sh`), NOT a convenient fake dict.


def _ssh_argv_for(tag: tuple[str, ...], *, gateway: str = "10.0.0.1", key: str = "/tmp/k") -> list:
    """Drive make_real_runner with a MOCKED ssh transport; return the argv it ran.

    Asserts exactly ONE subprocess.run happened (one SSH round-trip per mutating
    tag) and returns its argv so each test can assert the real contract.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        runner = system.make_real_runner(fw_gateway=gateway, fw_key=key)
        runner(tag)
    assert len(calls) == 1, f"expected exactly one SSH round-trip for {tag!r}, got {len(calls)}"
    return calls[0]


def _assert_ssh_shape(argv: list, *, key: str = "/tmp/k", gateway: str = "10.0.0.1") -> str:
    """Assert the SSH-options envelope (key-only, batch, bounded) and return the
    remote command string (the last argv element). Mirrors firewalla_wan_via_ssh."""
    assert argv[0] == "ssh"
    assert "-i" in argv and argv[argv.index("-i") + 1] == key
    assert "BatchMode=yes" in argv
    assert "PreferredAuthentications=publickey" in argv
    assert f"pi@{gateway}" in argv
    return argv[-1]


def test_make_real_runner_wan_dhcp_switches_wan_to_dhcp_over_ssh() -> None:
    argv = _ssh_argv_for(("wan_dhcp",))
    remote = _assert_ssh_shape(argv)
    # Switch WAN PPPoE->DHCP passthrough: derive the WAN dev from the default
    # route, then release + re-acquire a DHCP lease on it (the watchdog's
    # fallback_double_nat pattern, minus the hook removal — engaging, not reverting).
    assert "ip route show default" in remote
    assert "dhclient -r" in remote
    assert "dhclient " in remote


def test_make_real_runner_lease_observe_returns_the_downstream_wan_ip() -> None:
    # lease_observe must READ the downstream WAN lease and RETURN the IP for
    # classification — not fire-and-forget. The SSH stdout carries the address;
    # the runner parses it out (mirrors firewalla_wan_via_ssh's IP parse).
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="203.0.113.7\n", stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        runner = system.make_real_runner(fw_gateway="10.0.0.1", fw_key="/tmp/k")
        out = runner(("lease_observe",))
    assert out == "203.0.113.7"


def test_make_real_runner_lease_observe_reads_wan_ip_over_ssh() -> None:
    argv = _ssh_argv_for(("lease_observe",))
    remote = _assert_ssh_shape(argv)
    # Reads the WAN interface's primary IPv4 (the verify.sh WANIP capture shape).
    assert "ip" in remote and "addr show" in remote
    assert "dhclient" not in remote  # pure read — never mutates the lease


def test_make_real_runner_dhcp_release_re_leases_over_ssh() -> None:
    argv = _ssh_argv_for(("dhcp_release",))
    remote = _assert_ssh_shape(argv)
    # Re-lease the Firewalla WAN: release then re-acquire (watchdog fallback).
    assert "dhclient -r" in remote
    assert "dhclient " in remote


def test_make_real_runner_armor_arm_bootstraps_persistence_over_ssh() -> None:
    argv = _ssh_argv_for(("armor_arm",))
    remote = _assert_ssh_shape(argv)
    # Arm the boot-armor persistence (re-run post_main.sh, which (re)installs the
    # self-asserting DHCP hook + MTU clamp). The README's "run once" step.
    assert "post_main.sh" in remote


def test_make_real_runner_wan_addr_cidr_keeps_the_prefix_over_ssh() -> None:
    # FIX (c): the poison gate needs the /PREFIX + the route table that lease_observe
    # STRIPS. wan_addr_cidr reads `ip -4 -o addr show` and returns the RAW stdout
    # (with the /32-or-/1 prefix intact) so flip.evaluate_wan_poison can prove the
    # /32 armor is holding. Not the IPv4-only parse lease_observe does.
    argv = _ssh_argv_for(("wan_addr_cidr",))
    remote = _assert_ssh_shape(argv)
    assert "addr show" in remote
    assert "dhclient" not in remote  # pure read — never mutates the lease


def test_make_real_runner_wan_addr_cidr_returns_raw_stdout_with_prefix() -> None:
    # The raw readback must NOT be reduced to a bare IPv4 (that would discard the
    # /1-vs-/32 signal). It returns the SSH stdout verbatim, prefix and all.
    raw = "2: eth0    inet 24.150.33.7/32 brd 24.150.33.7 scope global eth0\n"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=raw, stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        runner = system.make_real_runner(fw_gateway="10.0.0.1", fw_key="/tmp/k")
        out = runner(("wan_addr_cidr",))
    assert out == raw  # full output, /32 preserved
    assert "/32" in out


def test_make_real_runner_wan_routes_returns_raw_route_table_over_ssh() -> None:
    # wan_routes surfaces the route table so a surviving 0.0.0.0/1 poison route is
    # visible to the gate. Returns raw stdout (the gate scans it line by line).
    raw = "default via 10.111.0.1 dev eth0\n0.0.0.0/1 via 10.111.0.1 dev eth0\n"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=raw, stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        runner = system.make_real_runner(fw_gateway="10.0.0.1", fw_key="/tmp/k")
        out = runner(("wan_routes",))
    assert out == raw
    assert "0.0.0.0/1" in out
    argv = _ssh_argv_for(("wan_routes",))
    remote = _assert_ssh_shape(argv)
    assert "route show" in remote
    assert "dhclient" not in remote


def test_make_real_runner_unknown_tag_on_apply_path_raises() -> None:
    # CRITICAL: an unknown/empty tag the apply path could fire must be a HARD
    # failure, never a silent "" no-op (which would report a green cutover while
    # the WAN was never touched and the rails' rollback never fired).
    runner = system.make_real_runner(fw_gateway="10.0.0.1", fw_key="/tmp/k")
    with pytest.raises(RuntimeError):
        runner(("definitely_not_a_real_tag",))
    with pytest.raises(RuntimeError):
        runner(())


def test_make_real_runner_mutating_tag_without_transport_raises() -> None:
    # A mutating tag with no fw gateway/key has no way to perform the WAN change;
    # silently returning "" would be the silent-no-op the council BLOCKED. Hard-fail.
    runner = system.make_real_runner(fw_gateway=None, fw_key=None)
    for tag in (
        ("wan_dhcp",),
        ("lease_observe",),
        ("dhcp_release",),
        ("armor_arm",),
        ("wan_addr_cidr",),
        ("wan_routes",),
    ):
        with pytest.raises(RuntimeError):
            runner(tag)


def test_make_real_runner_mutating_tag_raises_when_ssh_fails() -> None:
    # Fail-closed: a non-zero SSH exit on a mutating tag is a real failure the
    # orchestrator must see (so the rails roll back), never a swallowed "".
    def fail_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 255, stdout="", stderr="ssh: connect refused\n")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fail_run):
        runner = system.make_real_runner(fw_gateway="10.0.0.1", fw_key="/tmp/k")
        with pytest.raises(RuntimeError):
            runner(("wan_dhcp",))


def test_runner_implements_every_mutating_tag_the_orchestrator_fires() -> None:
    # Manifest guard (Contracts at the Boundary): the EXACT set of mutating tags
    # the single-NAT orchestrator fires must each resolve to a real command in
    # make_real_runner — derived from the PRODUCER's own constants, so a future
    # tag rename on one side without the other re-introduces the council-blocked
    # silent-no-op and fails here instead of in production.
    from sanctum_cli.devices import intents

    fired = {
        intents._RUNNER_WAN_DHCP,
        intents._RUNNER_LEASE_OBSERVE,
        intents._RUNNER_ARMOR_ARM,
        intents._RUNNER_DHCP_RELEASE,
        # FIX (c): the poison gate's raw readbacks (keep the /PREFIX + route table).
        intents._RUNNER_WAN_ADDR_CIDR,
        intents._RUNNER_WAN_ROUTES,
    }
    assert fired == set(system._FW_MUTATING_REMOTE)


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


def test_parse_speedtest_cli_json() -> None:
    blob = '{"download": {"bandwidth": 987500000}, "upload": {"bandwidth": 123000000}}'
    # bandwidth is bytes/sec -> *8/1e6 = Mbps
    assert system.parse_speedtest_cli_mbps(blob) == 7900.0


def test_parse_speedtest_cli_json_garbage_returns_none() -> None:
    assert system.parse_speedtest_cli_mbps("not json") is None
    assert system.parse_speedtest_cli_mbps("{}") is None


# ── FIX-f: firewalla_box_preflight — passwordless-sudo + dhclient over the SSH ─
#
# The pre-apply box gate's I/O boundary. It runs ONE key-SSH round-trip (the same
# _fw_ssh_argv envelope the cutover's box ops use) that echoes SUDO_OK / DHCLIENT_OK
# for each capability that holds. These author expectations from the marker contract,
# RUN the real argv construction, and mock ONLY the subprocess edge — fail-closed on
# any transport failure (the absence of proof reads as not-ready).


def test_box_preflight_both_markers_present_is_ready() -> None:
    """Both markers in stdout → (passwordless_sudo, dhclient_present) == (True, True)."""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SUDO_OK\nDHCLIENT_OK\n", stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        assert system.firewalla_box_preflight("10.0.0.1", "/k") == (True, True)


def test_box_preflight_missing_sudo_marker_reads_false() -> None:
    """Only DHCLIENT_OK (sudo needed a password, so no SUDO_OK) → sudo False."""

    def fake_run(cmd, **kwargs):
        # sudo -n true printed nothing (it failed); dhclient resolved.
        return subprocess.CompletedProcess(cmd, 0, stdout="DHCLIENT_OK\n", stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        assert system.firewalla_box_preflight("10.0.0.1", "/k") == (False, True)


def test_box_preflight_missing_dhclient_marker_reads_false() -> None:
    """Only SUDO_OK (no dhclient on PATH) → dhclient False."""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SUDO_OK\n", stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        assert system.firewalla_box_preflight("10.0.0.1", "/k") == (True, False)


def test_box_preflight_runs_over_the_fw_ssh_envelope() -> None:
    """The probe rides the SAME key-only SSH envelope the cutover's box ops use:
    publickey-only, accept-new host key, and the two capability probes in the remote."""
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="SUDO_OK\nDHCLIENT_OK\n", stderr="")

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=fake_run):
        system.firewalla_box_preflight("10.0.0.1", "/key", user="pi")
    argv = captured[0]
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "pi@10.0.0.1" in argv
    remote = argv[-1]
    assert "sudo -n true" in remote
    assert "dhclient" in remote


def test_box_preflight_transport_failure_is_fail_closed() -> None:
    """A transport failure (cannot spawn / timeout) reads as (False, False) — the
    absence of proof is not-ready, never a green pass on an unreachable box."""

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 12)

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=boom):
        assert system.firewalla_box_preflight("10.0.0.1", "/k") == (False, False)
