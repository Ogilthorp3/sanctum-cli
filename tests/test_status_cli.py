"""Tests for ``sanctum status`` backup & telemetry probes."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

from sanctum_cli import config
from sanctum_cli.commands import status

if TYPE_CHECKING:
    from pathlib import Path


def test_restic_timeout_allows_cloud_remote_roundtrip() -> None:
    assert status.RESTIC_TIMEOUT_S >= 20


def test_probe_backup_uses_compact_flag_and_parses_latest() -> None:
    captured: dict[str, object] = {}
    recent_iso = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat()

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        payload = json.dumps([{"id": "snap1", "time": recent_iso}])
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=payload, stderr="")

    with (
        patch("sanctum_cli.commands.status.shutil.which", return_value="/opt/homebrew/bin/restic"),
        patch("sanctum_cli.commands.status.subprocess.run", side_effect=fake_run),
    ):
        info = status.probe_backup(
            "rclone:gdrive-sanctum:sanctum-restic", {"RESTIC_PASSWORD": "pw"}
        )

    assert info.status == "OPERATIONAL"
    assert info.snapshot_count == 1
    assert info.age_human == "2h"
    assert info.detail is None
    assert "--compact" in captured["cmd"]  # type: ignore[operator]
    assert "--latest" in captured["cmd"]  # type: ignore[operator]
    assert "--no-lock" in captured["cmd"]  # type: ignore[operator]
    assert captured["timeout"] == status.RESTIC_TIMEOUT_S


def test_probe_backup_timeout_returns_unknown() -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=status.RESTIC_TIMEOUT_S)

    with (
        patch("sanctum_cli.commands.status.shutil.which", return_value="/opt/homebrew/bin/restic"),
        patch("sanctum_cli.commands.status.subprocess.run", side_effect=fake_run),
    ):
        info = status.probe_backup("rclone:gdrive-sanctum:sanctum-restic", {})

    assert info.status == "UNKNOWN"
    assert info.detail == "restic timed out"


def test_collect_probes_primary_and_secondary_preserving_order(full_instance_yaml: Path) -> None:
    cfg = config.load(full_instance_yaml)
    recent_iso = (datetime.now(tz=UTC) - timedelta(hours=3)).isoformat()

    def fake_probe_backup(repo: str, env: dict[str, str]) -> status.BackupInfo:
        assert env.get("RESTIC_PASSWORD") == "secret-pw"
        return status.BackupInfo(
            repo=repo,
            last_snapshot_iso=recent_iso,
            age_human="3h",
            snapshot_count=5,
            status="OPERATIONAL",
            detail=None,
        )

    with (
        patch("sanctum_cli.keychain.read", return_value="secret-pw") as kc,
        patch("sanctum_cli.commands.status.probe_backup", side_effect=fake_probe_backup),
    ):
        snap = status.collect(cfg)

    # Shared keychain ref across primary & secondary is read only once.
    kc.assert_called_once_with(account="sanctum-backup", service="sanctum-backup-key")
    repos = [b.repo for b in snap["backups"]]
    assert repos == [
        "/Volumes/T9/sanctum-restic",
        "rclone:gdrive-sanctum:sanctum-restic",
    ]
