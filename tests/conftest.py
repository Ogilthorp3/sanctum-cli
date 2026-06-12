"""Shared pytest fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def minimal_instance_yaml(tmp_path: Path) -> Path:
    """A tiny but valid instance.yaml — instance block only, defaults everywhere else."""
    p = tmp_path / "instance.yaml"
    p.write_text(
        "instance:\n"
        "  name: Test Instance\n"
        "  slug: test-instance\n"
        "  timezone: UTC\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def full_instance_yaml(tmp_path: Path) -> Path:
    """instance.yaml exercising every cli: subkey."""
    p = tmp_path / "instance.yaml"
    p.write_text(
        """\
instance:
  name: Manoir
  slug: manoir
  timezone: America/New_York

notifications:
  owner_name: Test Operator
  signal:
    enabled: true
    target: '+15555550100'

cli:
  default_provider: claude
  routing:
    rules:
      - when: { has_image: true }
        then: gemini
      - when: { intent: code }
        then: claude
    fallback: claude
  providers:
    claude:
      via: direct
      endpoint: https://api.anthropic.com
      model: claude-opus-4-7
    gemini:
      model: gemini-2.5-pro
    mlx_local:
      endpoint: http://127.0.0.1:8900
      always_available: true
  telemetry:
    enabled: true
    redact_prompts: true
    aggregate_window_days: 7
  cloud_backup:
    primary:
      kind: restic
      repo: /Volumes/T9/sanctum-restic
      keychain:
        service: sanctum-backup-key
        account: sanctum-backup
    secondary:
      kind: restic
      repo: rclone:gdrive-sanctum:sanctum-restic
      keychain:
        service: sanctum-backup-key
        account: sanctum-backup
""",
        encoding="utf-8",
    )
    return p
