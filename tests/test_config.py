"""Boundary tests for config loading and validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sanctum_cli import config
from sanctum_cli.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path


def test_load_minimal(minimal_instance_yaml: Path) -> None:
    cfg = config.load(minimal_instance_yaml)
    assert cfg.instance.name == "Test Instance"
    assert cfg.instance.slug == "test-instance"
    assert cfg.cli.default_provider == "claude"
    assert cfg.cli.routing.fallback == "claude"
    assert cfg.cli.telemetry.enabled is True


def test_load_full(full_instance_yaml: Path) -> None:
    cfg = config.load(full_instance_yaml)
    assert cfg.cli.cloud_backup is not None
    assert cfg.cli.cloud_backup.primary is not None
    assert cfg.cli.cloud_backup.primary.repo == "/Volumes/T9/sanctum-restic"
    assert len(cfg.cli.routing.rules) == 2
    assert cfg.cli.routing.rules[0].then == "gemini"


def test_missing_file_raises_with_fix(tmp_path: Path) -> None:
    target = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "not found" in ei.value.message
    assert ei.value.fix is not None
    assert "instance:" in ei.value.fix


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    target = tmp_path / "bad.yaml"
    target.write_text("instance: : :\n  not valid", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "YAML parse error" in ei.value.message


def test_unknown_key_in_cli_block_rejected(tmp_path: Path) -> None:
    target = tmp_path / "extra.yaml"
    target.write_text(
        "instance:\n  name: t\n  slug: t\ncli:\n  bogus_key: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "bogus_key" in ei.value.message


def test_invalid_provider_choice_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad-provider.yaml"
    target.write_text(
        "instance:\n  name: t\n  slug: t\ncli:\n  default_provider: openai\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "default_provider" in ei.value.message


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    target = tmp_path / "list.yaml"
    target.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "mapping" in ei.value.message.lower()


def test_env_override_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere.yaml"
    target.write_text("instance:\n  name: e\n  slug: e\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))
    cfg = config.load()
    assert cfg.instance.slug == "e"


def test_telemetry_path_is_expanded(tmp_path: Path) -> None:
    target = tmp_path / "tel.yaml"
    target.write_text(
        "instance:\n  name: t\n  slug: t\ncli:\n  telemetry:\n    path: ~/.sanctum/cli.jsonl\n",
        encoding="utf-8",
    )
    cfg = config.load(target)
    assert "~" not in str(cfg.cli.telemetry.path)
