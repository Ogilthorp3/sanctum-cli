"""No-config first-run safety — the gap that let the v0.9.0 blocker ship.

The existing onboard/status tests always pre-seed an instance.yaml fixture, so
they never exercised the truly-fresh-machine path where
``~/.sanctum/instance.yaml`` does not exist. That path was exactly where every
primary command died exit 5 (ConfigError). These tests point
``$SANCTUM_INSTANCE_FILE`` at a path that does NOT exist and assert a helpful,
non-crashing outcome.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sanctum_cli import cli
from sanctum_cli.cli import app
from sanctum_cli.commands import council as council_cmd
from sanctum_cli.commands import onboard

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
def missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at an instance.yaml that does not exist."""
    target = tmp_path / "does-not-exist" / "instance.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))
    assert not target.exists()
    return target


def test_status_no_config_is_friendly_not_crash(missing_config: Path) -> None:
    """`sanctum status` on a fresh box nudges instead of exit-5 ConfigError."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "not set up" in combined
    assert "sanctum init" in combined or "sanctum onboard" in combined


def test_status_json_no_config_reports_unconfigured(missing_config: Path) -> None:
    """JSON status stays machine-parseable on a fresh box (no traceback)."""
    import json

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    assert payload["configured"] is False
    assert "hint" in payload


def test_bare_invocation_no_config_does_not_crash(
    missing_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare ``sanctum`` (non-TTY) prints the nudge and exits clean."""
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: False)
    monkeypatch.setattr(council_cmd, "_repl", lambda: pytest.fail("must not enter REPL"))
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "not set up" in (result.stdout + (result.stderr or "")).lower()


def test_onboard_no_config_self_bootstraps(
    missing_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sanctum onboard` scaffolds the missing config instead of crashing exit-5.

    The documented first command must not die with ConfigError on a brand-new
    Mac. With every downstream op mocked, onboard scaffolds instance.yaml via
    config.ensure() and completes the flow (exit 0). The decisive assertion is
    that the file the loader needs now exists where it didn't before.
    """
    monkeypatch.setattr(
        "sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None
    )
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch(
            "sanctum_cli.commands.onboard._run_canary",
            return_value=onboard.CanaryOutcome.VERIFIED,
        ),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert missing_config.exists(), "onboard must scaffold the missing instance.yaml"
