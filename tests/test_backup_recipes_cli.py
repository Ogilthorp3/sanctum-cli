"""End-to-end tests for `sanctum backup recipes / estimate / run --recipe`."""

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


def _completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# ─── recipes (list) ─────────────────────────────────────────────────


def test_recipes_lists_built_ins(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["backup", "recipes", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload) >= {"family", "operator", "code"}
    assert payload["family"]["target"] == "primary"
    assert payload["family"]["user_override"] is False


def test_recipes_marks_user_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "instance.yaml"
    target.write_text(
        "instance:\n  name: T\n  slug: t\n"
        "cli:\n"
        "  recipes:\n"
        "    family:\n"
        "      description: my custom\n"
        "      sources: ['~/MyDocs']\n"
        "      target: primary\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))
    result = runner.invoke(app, ["backup", "recipes", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["family"]["user_override"] is True
    assert payload["operator"]["user_override"] is False  # still built-in


# ─── estimate ────────────────────────────────────────────────────────


def test_estimate_computes_total_and_compares_to_free_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The estimate command should run du, sum, and compare to 10 GB."""
    target = tmp_path / "instance.yaml"
    target.write_text(
        f"instance:\n  name: T\n  slug: t\n"
        f"cli:\n"
        f"  recipes:\n"
        f"    tiny:\n"
        f"      description: t\n"
        f"      sources: ['{tmp_path}']\n"
        f"      target: primary\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    # Seed a known-size file so du has something deterministic to measure.
    (tmp_path / "data").write_bytes(b"x" * 1024)

    result = runner.invoke(app, ["backup", "estimate", "tiny", "--json"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    payload = json.loads(result.stdout)
    assert payload["recipe"] == "tiny"
    assert payload["fits_r2_free_tier"] is True
    assert payload["free_tier_gb"] == 10
    assert payload["total_kb"] >= 0


def test_estimate_unknown_recipe_user_error(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["backup", "estimate", "no-such-thing"])
    assert result.exit_code == 1  # USER_ERROR


# ─── run --recipe ───────────────────────────────────────────────────


def test_run_recipe_requires_cloud_backup(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cloud_backup → typed UserError with fix."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["backup", "run", "--recipe", "family"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "cloud_backup" in combined.lower() or "cloud setup" in combined.lower()


def test_run_recipe_invokes_restic_with_excludes_file(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    # Override `family` recipe to point at a tmpdir source so the in-CLI
    # backup path runs without touching real user data.
    test_yaml = tmp_path / "instance.yaml"
    full_text = full_instance_yaml.read_text()
    test_yaml.write_text(
        full_text
        + "\n  recipes:\n    test-tiny:\n      description: t\n      sources:\n"
        f"        - '{tmp_path}'\n      target: primary\n      auto_exclude_icloud_photos: false\n"
        "  default_recipe: test-tiny\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(test_yaml))

    captured: dict[str, list[str]] = {}

    def fake_popen(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        import io

        captured["cmd"] = cmd

        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.StringIO("processed 0 files\n")
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:  # type: ignore[no-untyped-def, override]
                return 0

        return FakeProc()

    with (
        patch("sanctum_cli.commands.backup.subprocess.Popen", side_effect=fake_popen),
        patch("sanctum_cli.commands.backup.shutil.which", return_value="/usr/local/bin/restic"),
        patch("sanctum_cli.commands.backup.keychain.read", return_value="pwd"),
    ):
        result = runner.invoke(app, ["backup", "run", "--recipe", "test-tiny"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cmd = captured["cmd"]
    assert cmd[0] == "restic"
    assert "backup" in cmd
    assert "--exclude-file" in cmd
    assert "--exclude-caches" in cmd
    # tag includes the recipe name
    assert any(arg.startswith("recipe:") for arg in cmd)


def test_run_recipe_dry_run_passes_flag(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    test_yaml = tmp_path / "instance.yaml"
    full_text = full_instance_yaml.read_text()
    test_yaml.write_text(
        full_text
        + "\n  recipes:\n    test-tiny:\n      description: t\n      sources:\n"
        f"        - '{tmp_path}'\n      target: primary\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(test_yaml))

    captured: dict[str, list[str]] = {}

    def fake_popen(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        import io

        captured["cmd"] = cmd

        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.StringIO("")
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:  # type: ignore[no-untyped-def, override]
                return 0

        return FakeProc()

    with (
        patch("sanctum_cli.commands.backup.subprocess.Popen", side_effect=fake_popen),
        patch("sanctum_cli.commands.backup.shutil.which", return_value="/usr/local/bin/restic"),
        patch("sanctum_cli.commands.backup.keychain.read", return_value="pwd"),
    ):
        result = runner.invoke(app, ["backup", "run", "--recipe", "test-tiny", "--dry-run"])

    assert result.exit_code == 0
    assert "--dry-run" in captured["cmd"]


def test_run_no_recipe_falls_back_to_legacy_script(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without --recipe, the command should call the legacy bash script."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    fake_script = tmp_path / "backup.sh"
    fake_script.write_text("#!/bin/bash\necho legacy ran\n", encoding="utf-8")
    fake_script.chmod(0o755)

    invoked: dict[str, list[str]] = {}

    def fake_popen(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        import io

        invoked["cmd"] = cmd

        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.StringIO("legacy ran\n")
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:  # type: ignore[no-untyped-def, override]
                return 0

        return FakeProc()

    with patch("sanctum_cli.commands.backup.subprocess.Popen", side_effect=fake_popen):
        result = runner.invoke(app, ["backup", "run", "--script", str(fake_script)])

    assert result.exit_code == 0
    # First two args should be /bin/bash <script>
    assert invoked["cmd"][0] == "/bin/bash"
    assert str(fake_script) in invoked["cmd"]
