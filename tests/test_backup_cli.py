"""Tests for ``sanctum backup`` subcommands — subprocess + Keychain mocked."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_backup_snapshots_lists_from_both_repos(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    snaps_primary = json.dumps(
        [{"short_id": "abc12345", "time": "2026-04-26T17:40:19+00:00", "tags": ["daily"]}]
    )
    snaps_secondary = json.dumps(
        [{"short_id": "def67890", "time": "2026-04-26T21:37:39+00:00", "tags": ["daily", "gdrive"]}]
    )
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if "snapshots" in cmd:
            return _completed(stdout=snaps_primary if "T9" in cmd[2] else snaps_secondary)
        return _completed()

    with (
        patch("sanctum_cli.commands.backup.subprocess.run", side_effect=_run),
        patch("sanctum_cli.commands.backup.keychain.read", return_value="pwd"),
        patch("sanctum_cli.commands.backup.shutil.which", return_value="/usr/local/bin/restic"),
    ):
        result = runner.invoke(app, ["backup", "snapshots", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    repos = {entry["repo"] for entry in payload}
    assert repos == {"primary", "secondary"}
    ids = {entry["id"] for entry in payload}
    assert ids == {"abc12345", "def67890"}


def test_backup_snapshots_only_primary(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    def _run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _completed(stdout='[{"short_id":"a1","time":"2026-04-26T00:00:00+00:00"}]')

    with (
        patch("sanctum_cli.commands.backup.subprocess.run", side_effect=_run),
        patch("sanctum_cli.commands.backup.keychain.read", return_value="pwd"),
        patch("sanctum_cli.commands.backup.shutil.which", return_value="/usr/local/bin/restic"),
    ):
        result = runner.invoke(app, ["backup", "snapshots", "--repo", "primary", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert all(entry["repo"] == "primary" for entry in payload)


def test_backup_snapshots_no_cloud_backup_returns_user_error(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["backup", "snapshots"])
    assert result.exit_code == 1  # USER_ERROR
    combined = result.stdout + (result.stderr or "")
    assert "no cloud_backup" in combined.lower() or "cloud setup" in combined.lower()


def test_backup_verify_succeeds_for_all_repos(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    with (
        patch("sanctum_cli.commands.backup.subprocess.run", return_value=_completed()),
        patch("sanctum_cli.commands.backup.keychain.read", return_value="pwd"),
        patch("sanctum_cli.commands.backup.shutil.which", return_value="/usr/local/bin/restic"),
    ):
        result = runner.invoke(app, ["backup", "verify"])

    assert result.exit_code == 0, result.stdout
    assert "verified" in result.stdout.lower()


def test_backup_verify_failure_returns_local_error(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    with (
        patch(
            "sanctum_cli.commands.backup.subprocess.run",
            return_value=_completed(returncode=1, stderr="corrupted"),
        ),
        patch("sanctum_cli.commands.backup.keychain.read", return_value="pwd"),
        patch("sanctum_cli.commands.backup.shutil.which", return_value="/usr/local/bin/restic"),
    ):
        result = runner.invoke(app, ["backup", "verify"])
    assert result.exit_code == 4  # LOCAL_ERROR
    combined = result.stdout + (result.stderr or "")
    assert "verification failed" in combined.lower() or "rebuild-index" in combined.lower()


def test_backup_invalid_repo_filter_user_error(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    with (
        patch("sanctum_cli.commands.backup.keychain.read", return_value="pwd"),
        patch("sanctum_cli.commands.backup.shutil.which", return_value="/usr/local/bin/restic"),
    ):
        result = runner.invoke(app, ["backup", "snapshots", "--repo", "tertiary"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "primary" in combined.lower()


# ── _restic_env injects cloud creds by repo scheme (M8 fix) ──────────────
def test_restic_env_injects_b2_creds(monkeypatch):
    from sanctum_cli.commands import backup

    monkeypatch.setattr(backup, "_load_password", lambda cfg: "pw")
    monkeypatch.setattr(backup.keychain, "read", lambda account, service: f"{service}:val")
    env = backup._restic_env(None, backup._Repo(label="primary", path="b2:bucket"))
    assert env["RESTIC_PASSWORD"] == "pw"
    assert env["B2_ACCOUNT_ID"] == "b2-account-id:val"
    assert env["B2_ACCOUNT_KEY"] == "b2-application-key:val"
    assert "AWS_ACCESS_KEY_ID" not in env


def test_restic_env_injects_r2_s3_creds(monkeypatch):
    from sanctum_cli.commands import backup

    monkeypatch.setattr(backup, "_load_password", lambda cfg: "pw")
    monkeypatch.setattr(backup.keychain, "read", lambda account, service: f"{service}:val")
    for scheme in ("r2:bucket", "s3:bucket"):
        env = backup._restic_env(None, backup._Repo(label="primary", path=scheme))
        assert env["AWS_ACCESS_KEY_ID"] == "r2-access-key-id:val"
        assert env["AWS_SECRET_ACCESS_KEY"] == "r2-secret-access-key:val"
        assert env["AWS_DEFAULT_REGION"] == "auto"
        assert "B2_ACCOUNT_ID" not in env


def test_restic_env_local_repo_password_only(monkeypatch):
    from sanctum_cli.commands import backup

    monkeypatch.setattr(backup, "_load_password", lambda cfg: "pw")
    monkeypatch.setattr(backup.keychain, "read", lambda account, service: "should-not-read")
    env = backup._restic_env(None, backup._Repo(label="primary", path="/local/restic"))
    assert env["RESTIC_PASSWORD"] == "pw"
    assert "B2_ACCOUNT_ID" not in env and "AWS_ACCESS_KEY_ID" not in env
