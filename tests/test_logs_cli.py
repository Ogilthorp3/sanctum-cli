"""Regression tests for ``sanctum logs`` (L-CLI-1)."""

from __future__ import annotations

from typer.testing import CliRunner

from sanctum_cli.cli import app

runner = CliRunner()


def test_logs_list_flag_works_without_service_arg() -> None:
    res = runner.invoke(app, ["logs", "--list"])
    assert res.exit_code == 0, res.output
    assert "Known services:" in res.output
    assert "r2d2" in res.output


def test_logs_list_positional_alias_still_works() -> None:
    res = runner.invoke(app, ["logs", "list"])
    assert res.exit_code == 0, res.output
    assert "Known services:" in res.output


def test_logs_without_service_or_list_exits_2() -> None:
    res = runner.invoke(app, ["logs"])
    assert res.exit_code == 2
    assert "missing service name" in res.output.lower()
