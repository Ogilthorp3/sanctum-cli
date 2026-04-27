"""Tests for ``sanctum doctor`` — agents/providers/repos all mocked."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.providers.base import HealthSnapshot, Provider

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


LAUNCHCTL_ALL_GREEN = """\
PID\tStatus\tLabel
2087\t0\tcom.sanctum.proxy
-\t0\tcom.sanctum.bridge
123\t0\tcom.sanctum.health-center
-\t0\tcom.apple.something-else
"""

LAUNCHCTL_WITH_DEGRADED = """\
PID\tStatus\tLabel
2087\t0\tcom.sanctum.proxy
-\t-9\tcom.sanctum.lmstudio-bridge
123\t0\tcom.sanctum.bridge
-\t0\tcom.apple.something-else
"""


class _Fake(Provider):
    name = "fake"
    capabilities = 0  # type: ignore[assignment]

    def __init__(self, ok: bool, detail: str | None = None) -> None:
        self._ok = ok
        self._detail = detail

    def chat(self, _m, _o):  # type: ignore[no-untyped-def]
        yield ""

    def health(self) -> HealthSnapshot:
        return HealthSnapshot(
            ok=self._ok, latency_ms=42 if self._ok else None,
            quota_remaining=None, detail=self._detail,
        )

    def cost(self, _u):  # type: ignore[no-untyped-def]
        from decimal import Decimal
        return Decimal(0)


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _patch_subprocess(launchctl_out: str = LAUNCHCTL_ALL_GREEN):  # type: ignore[no-untyped-def]
    """Helper: patches subprocess.run to handle launchctl + restic."""
    def _run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        if cmd[0].endswith("launchctl"):
            return _completed(stdout=launchctl_out)
        if cmd[0].endswith("restic"):
            return _completed(stdout='[{"id":"abc","time":"2026-04-26T20:00:00+00:00"}]')
        return _completed(returncode=1, stderr="unknown")
    return _run


def test_doctor_brief_when_all_green(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    fake = _Fake(ok=True)
    with (
        patch("sanctum_cli.commands.doctor.subprocess.run", side_effect=_patch_subprocess()),
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/launchctl"),
        patch("sanctum_cli.commands.doctor.make_provider", return_value=fake),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    # Brevity rule: one-line summary, no full tables
    assert "sanctum doctor:" in result.stdout
    assert "operational" in result.stdout.lower()
    assert "LaunchAgents (com.sanctum.*)" not in result.stdout


def test_doctor_expands_when_degraded(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    fake = _Fake(ok=True)
    with (
        patch(
            "sanctum_cli.commands.doctor.subprocess.run",
            side_effect=_patch_subprocess(LAUNCHCTL_WITH_DEGRADED),
        ),
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/launchctl"),
        patch("sanctum_cli.commands.doctor.make_provider", return_value=fake),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    # Real findings → expand to full tables
    assert "com.sanctum.lmstudio-bridge" in result.stdout
    combined = result.stdout + (result.stderr or "")
    assert "degraded" in combined.lower()


def test_doctor_full_renders_table(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    fake = _Fake(ok=True)
    with (
        patch("sanctum_cli.commands.doctor.subprocess.run", side_effect=_patch_subprocess()),
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/launchctl"),
        patch("sanctum_cli.commands.doctor.make_provider", return_value=fake),
    ):
        result = runner.invoke(app, ["doctor", "--full"])
    assert result.exit_code == 0
    assert "com.sanctum.proxy" in result.stdout
    # Filtered out the apple.* row
    assert "com.apple.something-else" not in result.stdout


def test_doctor_json_emits_machine_readable(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    fake = _Fake(ok=True)
    with (
        patch("sanctum_cli.commands.doctor.subprocess.run", side_effect=_patch_subprocess()),
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/launchctl"),
        patch("sanctum_cli.commands.doctor.make_provider", return_value=fake),
    ):
        result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "overall" in payload
    assert isinstance(payload["agents"], list)
    assert isinstance(payload["providers"], list)
    labels = [a["label"] for a in payload["agents"]]
    assert "com.sanctum.proxy" in labels
    assert "com.apple.something-else" not in labels


def test_doctor_failed_provider_degrades_overall(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    fake = _Fake(ok=False, detail="rate limit")
    with (
        patch("sanctum_cli.commands.doctor.subprocess.run", side_effect=_patch_subprocess()),
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/launchctl"),
        patch("sanctum_cli.commands.doctor.make_provider", return_value=fake),
    ):
        result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["overall"] == "FAILED"


def test_doctor_filters_only_sanctum_prefixed_agents(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    fake = _Fake(ok=True)
    with (
        patch("sanctum_cli.commands.doctor.subprocess.run", side_effect=_patch_subprocess()),
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/launchctl"),
        patch("sanctum_cli.commands.doctor.make_provider", return_value=fake),
    ):
        result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    labels = {a["label"] for a in payload["agents"]}
    assert all(label.startswith("com.sanctum.") for label in labels)
