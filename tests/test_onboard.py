"""sanctum onboard — composition test, all underlying ops mocked."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sanctum_cli import recipes
from sanctum_cli.cli import app
from sanctum_cli.commands import onboard

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_live_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Onboard tests must never probe a real Firewalla bridge.

    Default stance: unreachable (None), i.e. the screen-time module is not
    paired. Gate tests override this with their own fake fetcher.
    """
    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None)


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

        load.return_value = Config(instance=InstanceMetadata(name="t", slug="t"), cli=CliConfig())
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


# ── Firewalla compatibility gate (family recipe) ─────────────────────
#
# Expectations derived from the box firmware semantics already pinned in
# tests/test_screen_time_compat.py: router/dhcp = in-path (PASS), spoof =
# ARP enforcement (WARN + "keep Monitoring ON…" fix). The gate must skip —
# never block — when the optional screen-time module isn't paired, and run
# the assessment STRICT when the bridge answers.


def _bridge_info(
    model: str = "goldpro", mode: str = "router", ready: bool = True
) -> dict[str, Any]:
    return {
        "box": {"model": model, "modelDisplay": model.title(), "mode": mode},
        "capabilities": {"enforcement_ready": ready, "box_mode": mode},
    }


def _invoke_family_onboard() -> tuple[int, str]:
    """Run `onboard --recipe family --yes` with the backup primitives mocked.

    Returns (exit_code, whitespace-normalized stdout) — normalization makes
    phrase assertions immune to rich's 80-column word wrapping.
    """
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])
    return result.exit_code, " ".join(result.stdout.split())


def test_onboard_family_compat_skipped_when_bridge_absent(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bridge unreachable / no token → SKIPPED hint, onboarding continues."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    # autouse fixture already stubs the bridge as unreachable (None)
    code, out = _invoke_family_onboard()
    assert code == 0, out
    assert "Firewalla compatibility" in out
    assert "skipped" in out
    assert "screen-time module not paired yet" in out
    assert "run `sanctum screen-time compat` after pairing" in out
    assert "onboarding complete" in out  # the flow reached the end


def test_onboard_family_compat_all_pass(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-path Gold Pro with headroom → the step passes inside onboarding."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    def fake_fetch(path: str) -> dict[str, Any] | None:
        if path == "/info":
            return _bridge_info()
        if path.startswith("/policies"):
            return {"policies": [], "count": 25}
        raise AssertionError(f"unexpected fetch {path}")

    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", fake_fetch)
    code, out = _invoke_family_onboard()
    assert code == 0, out
    assert "Firewalla compatibility" in out
    assert "✓ compatible" in out
    assert "skipped — screen-time module not paired yet" not in out


def test_onboard_family_compat_warn_fails_step_in_strict_with_fix(
    full_instance_yaml: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spoof-mode box → strict promotes the WARN to a step failure, fix shown."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    # Hermetic: no devices.yaml on this host's real paths may leak in.
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(tmp_path / "missing-devices.yaml"))

    def fake_fetch(path: str) -> dict[str, Any] | None:
        if path == "/info":
            return _bridge_info(model="red", mode="spoof")
        if path.startswith("/policies"):
            return {"policies": [], "count": 10}
        if path.startswith("/host/"):
            return {"monitored": True}
        return None

    monkeypatch.setattr("sanctum_cli.commands.screen_time._fetch_bridge_json", fake_fetch)
    code, out = _invoke_family_onboard()
    # The STEP fails loudly; the RUN still completes — the backup already
    # succeeded and screen-time is an optional module.
    assert code == 0, out
    assert "✗" in out
    assert "compatibility WARN (strict)" in out
    assert "keep Monitoring ON for every kid device in the Firewalla app" in out
    assert "onboarding complete" in out


def test_firewalla_compat_gate_listed_in_family_recipe() -> None:
    """The gate is recipe-listed data, not a buried conditional."""
    assert "firewalla-compat" in onboard.RECIPE_GATES["family"]
    # Gates may only reference recipes that actually exist.
    assert set(onboard.RECIPE_GATES) <= set(recipes.BUILTINS)
