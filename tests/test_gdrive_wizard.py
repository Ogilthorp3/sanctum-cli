"""Tests for the Google Drive setup wizard.

Network (rclone, restic, httpx-via-b2 helpers reused) and prompts are
mocked. Verifies the regex contracts, the slot-promotion logic
(primary → secondary), and the full happy path with a clean
instance.yaml.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.backends import gdrive
from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()

VALID_CLIENT_ID = "1234567890-" + "a" * 32 + ".apps.googleusercontent.com"
VALID_CLIENT_SECRET = "GOCSPX-" + "a" * 30


def _completed(rc: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


def test_client_id_regex() -> None:
    assert gdrive.CLIENT_ID_RE.match(VALID_CLIENT_ID)
    assert not gdrive.CLIENT_ID_RE.match("not-a-google-client")
    assert not gdrive.CLIENT_ID_RE.match("foo.apps.googleusercontent.com")  # missing num prefix


def test_client_secret_regex() -> None:
    assert gdrive.CLIENT_SECRET_RE.match(VALID_CLIENT_SECRET)
    assert not gdrive.CLIENT_SECRET_RE.match("just-a-string")
    assert not gdrive.CLIENT_SECRET_RE.match("GOCSPX-short")  # too short


def test_full_wizard_happy_path(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        # All rclone subcommands and security calls succeed
        if "listremotes" in cmd:
            return _completed(stdout="")  # no existing remote
        return _completed()

    def fake_prompt(label, *, validator, hint, password=False, max_attempts=3):  # type: ignore[no-untyped-def]
        return VALID_CLIENT_ID if "client_id" in label else VALID_CLIENT_SECRET

    with (
        patch("sanctum_cli.backends.gdrive.subprocess.run", side_effect=fake_run),
        patch("sanctum_cli.backends.gdrive.shutil.which", return_value="/x"),
        patch("sanctum_cli.backends.gdrive._prompt_validated", side_effect=fake_prompt),
        patch("sanctum_cli.backends.gdrive.Confirm.ask", return_value=True),
        patch("sanctum_cli.backends.gdrive._round_trip", return_value=None),
        patch("sanctum_cli.backends.gdrive.webbrowser.open", return_value=True),
        patch("sanctum_cli.backends.gdrive.keychain.exists", return_value=True),
        patch("sanctum_cli.backends.gdrive.keychain.read", return_value="passphrase"),
    ):
        result = runner.invoke(
            app, ["cloud", "setup", "--backend", "gdrive", "--no-open", "--no-persist"]
        )

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "done" in result.stdout.lower() or "OAuth complete" in result.stdout


def test_wizard_promotes_to_secondary_when_primary_present(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When primary is already set (B2), the gdrive wizard fills secondary."""
    # Copy the full_instance_yaml content but strip secondary so this slot is open
    import yaml as _yaml

    src_text = full_instance_yaml.read_text()
    parsed = _yaml.safe_load(src_text)
    if parsed.get("cli", {}).get("cloud_backup", {}).get("secondary") is not None:
        parsed["cli"]["cloud_backup"].pop("secondary")
    target = tmp_path / "instance.yaml"
    target.write_text(_yaml.safe_dump(parsed), encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        if "listremotes" in cmd:
            return _completed(stdout="")
        return _completed()

    def fake_prompt(label, *, validator, hint, password=False, max_attempts=3):  # type: ignore[no-untyped-def]
        return VALID_CLIENT_ID if "client_id" in label else VALID_CLIENT_SECRET

    with (
        patch("sanctum_cli.backends.gdrive.subprocess.run", side_effect=fake_run),
        patch("sanctum_cli.backends.gdrive.shutil.which", return_value="/x"),
        patch("sanctum_cli.backends.gdrive._prompt_validated", side_effect=fake_prompt),
        patch("sanctum_cli.backends.gdrive.Confirm.ask", return_value=True),
        patch("sanctum_cli.backends.gdrive._round_trip", return_value=None),
        patch("sanctum_cli.backends.gdrive.webbrowser.open", return_value=True),
        patch("sanctum_cli.backends.gdrive.keychain.exists", return_value=True),
        patch("sanctum_cli.backends.gdrive.keychain.read", return_value="passphrase"),
    ):
        result = runner.invoke(
            app, ["cloud", "setup", "--backend", "gdrive", "--no-open"]
        )

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    parsed_after = _yaml.safe_load(target.read_text())
    secondary = parsed_after["cli"]["cloud_backup"]["secondary"]
    assert secondary["repo"].startswith("rclone:gdrive-sanctum:")


def test_wizard_rejects_when_both_slots_full(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """full_instance_yaml has BOTH primary + secondary set."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    with patch("sanctum_cli.backends.gdrive.shutil.which", return_value="/x"):
        result = runner.invoke(
            app, ["cloud", "setup", "--backend", "gdrive", "--no-open", "--no-persist"]
        )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "primary" in combined.lower() and "secondary" in combined.lower()


def test_wizard_preflight_rclone_missing(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    with patch("sanctum_cli.backends.gdrive.shutil.which", return_value=None):
        result = runner.invoke(
            app, ["cloud", "setup", "--backend", "gdrive", "--no-open", "--no-persist"]
        )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "rclone" in combined.lower()
