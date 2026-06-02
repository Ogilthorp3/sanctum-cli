"""Tests for `sanctum module list` and `sanctum module status`."""
from typer.testing import CliRunner
from sanctum_cli.cli import app

runner = CliRunner()


def test_module_list_shows_backup():
    r = runner.invoke(app, ["module", "list"])
    assert r.exit_code == 0
    assert "backup" in r.stdout


def test_module_status_unknown_errors():
    r = runner.invoke(app, ["module", "status", "ghost"])
    assert r.exit_code != 0
    assert "unknown module" in r.stdout.lower() or "ghost" in r.stdout
