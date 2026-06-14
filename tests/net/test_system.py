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
