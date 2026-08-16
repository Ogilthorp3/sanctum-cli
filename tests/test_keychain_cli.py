"""Tests for ``sanctum keychain`` — security + keychain.read both mocked."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_list_never_prints_values(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    with (
        patch("sanctum_cli.commands.keychain_cmd.keychain.exists", return_value=True),
    ):
        result = runner.invoke(app, ["keychain", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    services = {r["service"] for r in payload}
    assert "anthropic-api-key" in services
    assert "google-ai-api-key" in services
    # No "value" or "secret" key in the output ever
    assert all("value" not in r and "secret" not in r for r in payload)


def test_test_passes_when_all_readable(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    with patch("sanctum_cli.commands.keychain_cmd.keychain.read", return_value="x"):
        result = runner.invoke(app, ["keychain", "test"])
    assert result.exit_code == 0
    assert "✓" in result.stdout


def test_test_fails_loudly_when_unreadable(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    from sanctum_cli.errors import LocalError

    def raiser(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        msg = "Keychain locked"
        raise LocalError(msg)

    with patch("sanctum_cli.commands.keychain_cmd.keychain.read", side_effect=raiser):
        result = runner.invoke(app, ["keychain", "test"])
    assert result.exit_code == 4  # LOCAL_ERROR


def test_rotate_with_explicit_value_and_yes() -> None:
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return _completed()

    with (
        patch("sanctum_cli.commands.keychain_cmd.shutil.which", return_value="/usr/bin/security"),
        patch("sanctum_cli.commands.keychain_cmd.subprocess.run", side_effect=_run),
    ):
        result = runner.invoke(
            app,
            ["keychain", "rotate", "test-svc", "-a", "sanctum", "--value", "ROTATED", "-y"],
        )
    assert result.exit_code == 0
    args = calls[0]
    assert "-U" in args
    assert "test-svc" in args
    assert "ROTATED" in args


def test_rotate_auto_generates_64_hex_chars() -> None:
    captured: dict[str, str] = {}

    def _run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        # The value is the arg right after -w
        idx = cmd.index("-w")
        captured["value"] = cmd[idx + 1]
        return _completed()

    with (
        patch("sanctum_cli.commands.keychain_cmd.shutil.which", return_value="/usr/bin/security"),
        patch("sanctum_cli.commands.keychain_cmd.subprocess.run", side_effect=_run),
    ):
        result = runner.invoke(app, ["keychain", "rotate", "test-svc", "-y"])
    assert result.exit_code == 0
    assert len(captured["value"]) == 64
    assert all(c in "0123456789abcdef" for c in captured["value"])


def test_rotate_failure_returns_local_error() -> None:
    with (
        patch("sanctum_cli.commands.keychain_cmd.shutil.which", return_value="/usr/bin/security"),
        patch(
            "sanctum_cli.commands.keychain_cmd.subprocess.run",
            return_value=_completed(rc=99, stderr="boom"),
        ),
    ):
        result = runner.invoke(app, ["keychain", "rotate", "test-svc", "--value", "x", "-y"])
    assert result.exit_code == 4
    combined = result.stdout + (result.stderr or "")
    assert "rotation failed" in combined.lower() or "boom" in combined.lower()
