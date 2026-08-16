"""End-to-end CLI tests via Typer's testing harness."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "sanctum" in result.stdout


def test_help_lists_status_and_config() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout
    assert "config" in result.stdout


def test_config_validate_minimal(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.stdout
    assert "valid" in result.stdout.lower()


def test_config_validate_json(minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["config", "validate", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["instance"]["slug"] == "test-instance"


def test_config_validate_bad_yaml_returns_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("instance:\n  bogus: 1\n", encoding="utf-8")  # missing required name/slug
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(bad))
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 5  # ExitCode.CONFIG_ERROR
    combined = result.stdout + (result.stderr or "")
    assert "schema violation" in combined.lower() or "name" in combined


def test_config_validate_missing_returns_5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "nope.yaml"))
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 5


def test_status_oneline(minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["status", "--oneline"])
    assert result.exit_code == 0, result.stdout
    assert "sanctum" in result.stdout.lower()
    assert "router" in result.stdout.lower()


def test_status_json(minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["instance"] == "Test Instance"
    assert payload["default_provider"] == "claude"
    assert "disk" in payload
    assert "telemetry" in payload
