"""sanctum onboard — composition test, all underlying ops mocked."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def test_onboard_with_existing_cloud_skips_setup_and_runs_backup(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate") as estimate,
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run") as run_,
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup") as setup,
        patch("sanctum_cli.commands.onboard._run_canary") as canary,
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    estimate.assert_called_once()
    setup.assert_not_called()  # cloud_backup already configured
    # backup_run called twice: once with dry_run=True, once with dry_run=False
    assert run_.call_count == 2
    canary.assert_called_once()


def test_onboard_runs_setup_when_cloud_unconfigured(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))

    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup") as setup,
        patch("sanctum_cli.commands.onboard._run_canary"),
        patch("sanctum_cli.commands.onboard.config.load") as load,
    ):
        # First load: no cloud_backup; second load: simulate it after setup.
        # We don't actually mutate state; just confirm setup was called.
        from sanctum_cli.config import CliConfig, Config, InstanceMetadata

        load.return_value = Config(
            instance=InstanceMetadata(name="t", slug="t"), cli=CliConfig()
        )
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    setup.assert_called_once_with("r2", no_open=False)


def test_onboard_family_shows_photos_warning(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])
    assert result.exit_code == 0
    assert "iCloud" in result.stdout or "Photos" in result.stdout


def test_onboard_operator_skips_photos_warning(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "operator", "--yes"])
    assert result.exit_code == 0
    # The photos panel mentions iCloud — operator path should not.
    assert "Photos scope notice" not in result.stdout


def test_network_gear_check_skips_on_assume_yes() -> None:
    """--yes path must not prompt for or print the network-gear chapter."""
    from sanctum_cli.commands import onboard

    with patch("sanctum_cli.commands.onboard.Confirm.ask") as ask:
        onboard._network_gear_check(assume_yes=True)
    ask.assert_not_called()


def test_network_gear_check_prints_ap_dns_trap(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a downstream AP, the chapter teaches the empty-DNS trap + DHCP fix."""
    import io

    from rich.console import Console

    from sanctum_cli.commands import onboard

    buf = io.StringIO()
    monkeypatch.setattr(onboard, "console", Console(file=buf, width=100))
    with patch("sanctum_cli.commands.onboard.Confirm.ask", return_value=True):
        onboard._network_gear_check(assume_yes=False)
    out = buf.getvalue()
    assert "AP" in out  # AP / bridge mode
    assert "DNS" in out  # the empty-DNS trap
    assert "magenta" in out.lower()
    assert "DHCP" in out  # the recommended fix


def test_network_gear_check_silent_when_no_ap(monkeypatch: pytest.MonkeyPatch) -> None:
    """No downstream AP -> no panel, clean pass-through."""
    import io

    from rich.console import Console

    from sanctum_cli.commands import onboard

    buf = io.StringIO()
    monkeypatch.setattr(onboard, "console", Console(file=buf, width=100))
    with patch("sanctum_cli.commands.onboard.Confirm.ask", return_value=False):
        onboard._network_gear_check(assume_yes=False)
    assert buf.getvalue().strip() == ""
