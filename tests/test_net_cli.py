from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app
from tests.net import fixtures as fx

runner = CliRunner()


def test_net_check_reports_double_nat() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.DOUBLE_NAT)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "Bell")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=True),
    ):
        result = runner.invoke(app, ["net", "check"])
    assert result.exit_code == 0, result.stdout
    assert "double" in result.stdout.lower()


def test_net_check_single_nat_says_optimal() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.SINGLE_NAT)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "Bell")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=True),
    ):
        result = runner.invoke(app, ["net", "check"])
    assert result.exit_code == 0
    assert "optimal" in result.stdout.lower() or "already" in result.stdout.lower()


def test_net_optimize_not_applicable_no_firewalla_exits_clean() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.NO_FIREWALLA)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=False),
    ):
        result = runner.invoke(app, ["net", "optimize", "--yes"])
    assert result.exit_code == 0
    assert "nothing to optimize" in result.stdout.lower()


def test_net_optimize_double_nat_prints_plan() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.DOUBLE_NAT)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "Bell")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=True),
    ):
        result = runner.invoke(app, ["net", "optimize", "--yes", "--plan-only"])
    assert result.exit_code == 0, result.stdout
    assert "20:6d:31:51:67:82" in result.stdout
    assert "Advanced DMZ" in result.stdout
