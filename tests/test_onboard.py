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
