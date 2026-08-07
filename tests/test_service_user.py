"""Tests for hive service principal (wave-1) checks."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sanctum_cli import service_user as su
from sanctum_cli.cli import app
from sanctum_cli.commands import service_user_cmd  # noqa: F401 — register


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_cli_only_not_applicable(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "haus_tier_present", lambda: False)
    report = su.check_wave1()
    assert report.applicable is False
    assert report.ok is True


def test_wave1_fail_when_user_missing(monkeypatch):
    monkeypatch.setattr(su, "haus_tier_present", lambda: True)
    monkeypatch.setattr(su, "service_user_exists", lambda name="sanctum": False)
    monkeypatch.setattr(su, "_plist_username", lambda p: None)
    monkeypatch.setattr(su, "_process_owner", lambda p: None)
    monkeypatch.setattr(su, "_http_status", lambda *a, **k: 0)
    monkeypatch.setattr(su, "DAEMON_DIR", Path("/nonexistent/LaunchDaemons"))
    report = su.check_wave1()
    assert report.applicable is True
    assert report.ok is False
    assert any("sanctum user exists" in i.name and not i.ok for i in report.items)


def test_status_command_cli_only(runner, monkeypatch):
    monkeypatch.setattr(su, "haus_tier_present", lambda: False)
    result = runner.invoke(app, ["service-user", "status"])
    assert result.exit_code == 0
    assert "n/a" in result.output or "not expected" in result.output


def test_check_exits_one_when_unhealthy(runner, monkeypatch):
    bad = su.Wave1Report(
        applicable=True,
        items=[su.CheckItem("sanctum user exists", False, "missing")],
    )
    monkeypatch.setattr(su, "check_wave1", lambda **k: bad)
    result = runner.invoke(app, ["service-user", "check"])
    assert result.exit_code == 1


def test_check_exits_zero_when_ok(runner, monkeypatch):
    good = su.Wave1Report(
        applicable=True,
        items=[su.CheckItem("all", True, "ok")],
    )
    monkeypatch.setattr(su, "check_wave1", lambda **k: good)
    result = runner.invoke(app, ["service-user", "check"])
    assert result.exit_code == 0


def test_install_dry_run(runner, monkeypatch, tmp_path):
    script = tmp_path / "install-on-new-hub.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(su, "install_script_path", lambda: script)
    result = runner.invoke(app, ["service-user", "install", "--dry-run"])
    assert result.exit_code == 0
    assert "would run" in result.output


def test_onboard_operator_lists_service_user_gate():
    from sanctum_cli.commands import onboard

    assert "service-user-install" in onboard.RECIPE_GATES["operator"]
    assert "service-user-install" not in onboard.RECIPE_GATES["family"]
    assert "service-user-install" in onboard._CHAPTER_GATES["Your Network"]
    assert "service-user-install" in onboard._GATE_LABELS
