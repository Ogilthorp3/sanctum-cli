"""``sanctum init`` — first-run config bootstrap command.

The v0.9.0 onboarding blocker: a fresh machine had no way to create
``~/.sanctum/instance.yaml`` short of hand-writing YAML, so every primary
command died exit 5 (ConfigError) on first run. ``sanctum init`` writes the
smallest file the strict loader accepts and is idempotent by default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from sanctum_cli import config
from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def test_init_creates_loadable_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh ``init --yes`` writes a file the strict loader accepts."""
    target = tmp_path / "instance.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))
    assert not target.exists()

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert target.exists()
    cfg = config.load(target)  # would raise ConfigError if invalid
    assert cfg.instance.name
    assert cfg.instance.slug


def test_init_honors_env_instance_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """init writes to ``$SANCTUM_INSTANCE_FILE``, not the default location."""
    target = tmp_path / "nested" / "custom.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert target.exists(), "init must honor SANCTUM_INSTANCE_FILE"


def test_init_is_idempotent_does_not_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running init leaves an existing file untouched and exits 0."""
    target = tmp_path / "instance.yaml"
    target.write_text("instance:\n  name: Existing Haus\n  slug: existing-haus\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "already exists" in (result.stdout + (result.stderr or "")).lower()
    assert config.load(target).instance.name == "Existing Haus"


def test_init_force_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--force replaces an existing file with a freshly named one."""
    target = tmp_path / "instance.yaml"
    target.write_text("instance:\n  name: Old\n  slug: old\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--name", "New Haus", "--force"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cfg = config.load(target)
    assert cfg.instance.name == "New Haus"
    assert cfg.instance.slug == "new-haus"


def test_init_name_derives_safe_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A messy --name still yields a lowercase, bucket-safe slug."""
    target = tmp_path / "instance.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--name", "Bert's Haus!! 2026"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cfg = config.load(target)
    assert cfg.instance.slug == cfg.instance.slug.lower()
    assert all(c.isalnum() or c == "-" for c in cfg.instance.slug)
    assert cfg.instance.slug, "slug must not be empty even from a punctuation-heavy name"
