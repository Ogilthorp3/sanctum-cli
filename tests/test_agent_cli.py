"""Tests for ``sanctum agent`` — launchctl + plist boundaries mocked."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import agent

if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()


LAUNCHCTL_OUT = """\
PID\tStatus\tLabel
2741\t0\tcom.sanctum.admit
-\t0\tcom.sanctum.bridge
123\t0\tcom.sanctum.health-center
-\t-9\tcom.sanctum.lmstudio-bridge
-\t0\tcom.apple.something-else
"""


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_compute_status_branches() -> None:
    assert agent._compute_status("123", "0") == "RUNNING"
    assert agent._compute_status("-", "0") == "LOADED"
    assert agent._compute_status("-", "-9") == "FAILED"
    assert agent._compute_status("-", "not-int") == "LOADED"


def test_list_filters_to_sanctum() -> None:
    with (
        patch("sanctum_cli.commands.agent.shutil.which", return_value="/bin/launchctl"),
        patch(
            "sanctum_cli.commands.agent.subprocess.run",
            return_value=_completed(stdout=LAUNCHCTL_OUT),
        ),
    ):
        result = runner.invoke(app, ["agent", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    labels = [r["label"] for r in payload]
    assert "com.sanctum.admit" in labels
    assert "com.sanctum.lmstudio-bridge" in labels
    assert all(label.startswith("com.sanctum.") for label in labels)
    assert any(r["status"] == "FAILED" for r in payload)


def test_status_for_loaded_agent_renders_path(
    tmp_path: Path,
) -> None:
    plist = tmp_path / "com.sanctum.demo.plist"
    plist.write_bytes(
        b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.sanctum.demo</string>
<key>StandardOutPath</key><string>/tmp/demo.log</string>
</dict></plist>"""
    )

    def _which(name):  # type: ignore[no-untyped-def]
        # Force the plutil fallback so plistlib reads the plist directly.
        if "plutil" in name:
            return None
        return "/bin/launchctl"

    def _run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _completed(stdout="PID\tStatus\tLabel\n42\t0\tcom.sanctum.demo\n")

    with (
        patch("sanctum_cli.commands.agent.shutil.which", side_effect=_which),
        patch("sanctum_cli.commands.agent.subprocess.run", side_effect=_run),
        patch("sanctum_cli.commands.agent.PLIST_LOCATIONS", [tmp_path]),
    ):
        result = runner.invoke(app, ["agent", "status", "com.sanctum.demo"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "com.sanctum.demo" in result.stdout
    assert "/tmp/demo.log" in result.stdout
    assert "RUNNING" in result.stdout


def test_status_missing_agent_user_error() -> None:
    with (
        patch("sanctum_cli.commands.agent.shutil.which", return_value="/bin/launchctl"),
        patch(
            "sanctum_cli.commands.agent.subprocess.run",
            return_value=_completed(stdout="PID\tStatus\tLabel\n"),
        ),
    ):
        result = runner.invoke(app, ["agent", "status", "com.sanctum.ghost"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "not loaded" in combined.lower() or "not in launchctl" in combined.lower()


def test_start_invokes_bootstrap(tmp_path: Path) -> None:
    plist = tmp_path / "com.sanctum.demo.plist"
    plist.write_bytes(
        b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>Label</key><string>com.sanctum.demo</string></dict></plist>"""
    )
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _completed()

    with (
        patch("sanctum_cli.commands.agent.subprocess.run", side_effect=_run),
        patch("sanctum_cli.commands.agent.PLIST_LOCATIONS", [tmp_path]),
    ):
        result = runner.invoke(app, ["agent", "start", "com.sanctum.demo"])
    assert result.exit_code == 0
    assert any("bootstrap" in cmd for cmd in calls)


def test_start_failure_returns_local_error(tmp_path: Path) -> None:
    plist = tmp_path / "com.sanctum.demo.plist"
    plist.write_bytes(
        b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>Label</key><string>com.sanctum.demo</string></dict></plist>"""
    )
    with (
        patch(
            "sanctum_cli.commands.agent.subprocess.run",
            return_value=_completed(returncode=5, stderr="cannot bootstrap"),
        ),
        patch("sanctum_cli.commands.agent.PLIST_LOCATIONS", [tmp_path]),
    ):
        result = runner.invoke(app, ["agent", "start", "com.sanctum.demo"])
    assert result.exit_code == 4  # LOCAL_ERROR


def test_logs_tails_existing_file(tmp_path: Path) -> None:
    log = tmp_path / "demo.log"
    log.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    plist = tmp_path / "com.sanctum.demo.plist"
    plist.write_bytes(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.sanctum.demo</string>
<key>StandardOutPath</key><string>{log}</string>
</dict></plist>""".encode()
    )
    with patch("sanctum_cli.commands.agent.PLIST_LOCATIONS", [tmp_path]):
        result = runner.invoke(app, ["agent", "logs", "com.sanctum.demo", "--lines", "2"])
    assert result.exit_code == 0
    assert "line3" in result.stdout
    assert "line4" in result.stdout
    assert "line1" not in result.stdout


def test_tail_lines_helper() -> None:
    import io

    fh = io.StringIO("a\nb\nc\nd\n")
    assert agent._tail_lines(fh, 2) == ["c\n", "d\n"]
