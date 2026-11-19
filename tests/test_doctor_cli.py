"""Tests for ``sanctum doctor`` — agents/providers/repos all mocked."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.providers.base import HealthSnapshot, Provider

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _daemon_probes_are_hermetic():  # type: ignore[no-untyped-def]
    """``doctor`` probes force-flow/thalamus over local HTTP. Pin the probe so these
    tests do not depend on a service listening on the machine that runs them."""
    with patch("sanctum_cli.commands.doctor._http_ok", return_value=True):
        yield


LAUNCHCTL_ALL_GREEN = """\
PID\tStatus\tLabel
2087\t0\thaus.sanctum.proxy
-\t0\thaus.sanctum.bridge
123\t0\thaus.sanctum.health-center
-\t0\tcom.apple.something-else
"""

LAUNCHCTL_WITH_DEGRADED = """\
PID\tStatus\tLabel
2087\t0\thaus.sanctum.proxy
-\t-9\thaus.sanctum.lmstudio-bridge
123\t0\thaus.sanctum.bridge
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
        patch("sanctum_cli.commands.doctor.haus_tier_present", return_value=False),
        patch("sanctum_cli.commands.doctor._http_ok", return_value=False),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    # Brevity rule: one-line summary, no full tables
    assert "sanctum doctor:" in result.stdout
    assert "operational" in result.stdout.lower()
    assert "LaunchAgents (haus.sanctum.*)" not in result.stdout


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
        patch("sanctum_cli.commands.doctor.haus_tier_present", return_value=False),
        patch("sanctum_cli.commands.doctor._http_ok", return_value=False),
    ):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    # Real findings → expand to full tables
    assert "haus.sanctum.lmstudio-bridge" in result.stdout
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
        patch("sanctum_cli.commands.doctor.haus_tier_present", return_value=False),
        patch("sanctum_cli.commands.doctor._http_ok", return_value=False),
    ):
        result = runner.invoke(app, ["doctor", "--full"])
    assert result.exit_code == 0
    assert "haus.sanctum.proxy" in result.stdout
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
        patch("sanctum_cli.commands.doctor.haus_tier_present", return_value=False),
        patch("sanctum_cli.commands.doctor._http_ok", return_value=False),
    ):
        result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "overall" in payload
    assert isinstance(payload["agents"], list)
    assert isinstance(payload["providers"], list)
    labels = [a["label"] for a in payload["agents"]]
    assert "haus.sanctum.proxy" in labels
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
        patch("sanctum_cli.commands.doctor.haus_tier_present", return_value=False),
        patch("sanctum_cli.commands.doctor._http_ok", return_value=False),
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
        patch("sanctum_cli.commands.doctor.haus_tier_present", return_value=False),
        patch("sanctum_cli.commands.doctor._http_ok", return_value=False),
    ):
        result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    labels = {a["label"] for a in payload["agents"]}
    assert all(label.startswith("haus.sanctum.") for label in labels)


def test_doctor_force_flow_expected_only_on_haus_tier() -> None:
    from sanctum_cli.commands import doctor

    with (
        patch("sanctum_cli.commands.doctor.subprocess.run", side_effect=_patch_subprocess()),
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/launchctl"),
        patch("sanctum_cli.commands.doctor.haus_tier_present", return_value=False),
        patch("sanctum_cli.commands.doctor._http_ok", return_value=False),
    ):
        rows_basic = doctor._agents()
    assert all(r.label != "haus.sanctum.force-flow" for r in rows_basic)
    assert all(r.status == "OPERATIONAL" for r in rows_basic)

    with (
        patch("sanctum_cli.commands.doctor.subprocess.run", side_effect=_patch_subprocess()),
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/launchctl"),
        patch("sanctum_cli.commands.doctor.haus_tier_present", return_value=True),
        patch("sanctum_cli.commands.doctor._http_ok", return_value=False),
    ):
        rows_haus = doctor._agents()
    ff = [r for r in rows_haus if r.label == "haus.sanctum.force-flow"]
    assert len(ff) == 1
    assert ff[0].status == "FAILED"


def test_doctor_repos_injects_cloud_creds_and_isolates_failures(tmp_path: Path) -> None:
    from sanctum_cli import config
    from sanctum_cli.commands import doctor
    from sanctum_cli.keychain import KeychainEntryMissingError

    inst = tmp_path / "instance.yaml"
    inst.write_text(
        """\
instance:
  name: Cloud Box
  slug: cloud-box
cli:
  cloud_backup:
    primary:
      kind: restic
      repo: s3:https://id.r2.cloudflarestorage.com/bucket
      keychain:
        service: primary-svc
        account: primary-acct
    secondary:
      kind: restic
      repo: b2:bucket
      keychain:
        service: missing-svc
        account: secondary-acct
""",
        encoding="utf-8",
    )
    cfg = config.load(inst)
    seen_envs: list[dict[str, str]] = []

    def fake_read(*, account: str, service: str) -> str:
        if service == "missing-svc":
            raise KeychainEntryMissingError("missing secondary")
        return f"{account}:{service}"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_envs.append(dict(kwargs["env"]))  # type: ignore[arg-type]
        return _completed(stdout="[]")

    with (
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/restic"),
        patch("sanctum_cli.keychain.read", side_effect=fake_read),
        patch("sanctum_cli.commands.doctor.subprocess.run", side_effect=fake_run),
    ):
        rows = doctor._repos(cfg)

    assert len(rows) == 2
    assert rows[0].status == "OPERATIONAL"
    assert seen_envs[0]["AWS_ACCESS_KEY_ID"] == "sanctum:r2-access-key-id"
    assert rows[1].status == "UNKNOWN"
    assert "keychain unavailable" in (rows[1].detail or "")


def test_doctor_source_has_no_hardcoded_operator_home() -> None:
    from pathlib import Path

    from sanctum_cli.commands import doctor

    assert "/Users/" + "be" + "rt" not in Path(doctor.__file__).read_text(encoding="utf-8")
