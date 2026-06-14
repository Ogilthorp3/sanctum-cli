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
        assert system.firewalla_wan_via_ssh("10.0.0.1", "/tmp/k") == ("9.9.9.9", "aa:bb:cc:dd:ee:ff")


def test_firewalla_wan_via_ssh_returns_empty_on_timeout() -> None:
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 12)

    with patch("sanctum_cli.net.system.subprocess.run", side_effect=boom):
        assert system.firewalla_wan_via_ssh("10.0.0.1", "/tmp/k") == ("", "")
