"""Tests for ``sanctum proxy`` — http + agent both mocked."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from sanctum_cli.cli import app

runner = CliRunner()


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


LAUNCHCTL_OUT = """\
PID\tStatus\tLabel
2087\t0\tcom.sanctum.claude-cli-proxy
-\t0\tcom.sanctum.server
-\t-9\tcom.sanctum.lmstudio-bridge
"""


def test_status_all_renders_each_proxy() -> None:
    def fake_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", "https://x"))

    with (
        patch("sanctum_cli.commands.proxy.httpx.get", side_effect=fake_get),
        patch("sanctum_cli.commands.agent.shutil.which", return_value="/bin/launchctl"),
        patch(
            "sanctum_cli.commands.agent.subprocess.run",
            return_value=_completed(stdout=LAUNCHCTL_OUT),
        ),
    ):
        result = runner.invoke(app, ["proxy", "status", "all", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    names = {r["name"] for r in payload}
    assert names == {"claude-cli-proxy", "sanctum-server", "lmstudio-bridge"}
    cli_proxy = next(r for r in payload if r["name"] == "claude-cli-proxy")
    assert cli_proxy["http_ok"] is True
    assert cli_proxy["agent"] == "RUNNING"


def test_status_unknown_target_user_error() -> None:
    result = runner.invoke(app, ["proxy", "status", "no-such-thing"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "unknown proxy" in combined.lower()


def test_status_http_failure_marks_down() -> None:
    def fake_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("nope")

    with (
        patch("sanctum_cli.commands.proxy.httpx.get", side_effect=fake_get),
        patch("sanctum_cli.commands.agent.shutil.which", return_value="/bin/launchctl"),
        patch(
            "sanctum_cli.commands.agent.subprocess.run",
            return_value=_completed(stdout=LAUNCHCTL_OUT),
        ),
    ):
        result = runner.invoke(app, ["proxy", "status", "claude-cli-proxy", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload[0]["http_ok"] is False
    assert "nope" in (payload[0]["detail"] or "")
