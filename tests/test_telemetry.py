"""Telemetry roundtrip + redaction tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from sanctum_cli.config import Telemetry as TelemetryConfig
from sanctum_cli.telemetry import Span, emit

if TYPE_CHECKING:
    from pathlib import Path


def _read_lines(p: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


def test_emit_writes_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "cli.jsonl"
    cfg = TelemetryConfig(path=p, redact_prompts=True, enabled=True)
    emit(cfg, command="status", status="ok", duration_ms=42)
    events = _read_lines(p)
    assert len(events) == 1
    e = events[0]
    assert e["command"] == "status"
    assert e["status"] == "ok"
    assert e["duration_ms"] == 42
    assert "ts" in e
    assert "host" in e


def test_emit_disabled_writes_nothing(tmp_path: Path) -> None:
    p = tmp_path / "cli.jsonl"
    cfg = TelemetryConfig(path=p, enabled=False)
    emit(cfg, command="x", status="ok", duration_ms=0)
    assert not p.exists()


def test_prompt_is_redacted_by_default(tmp_path: Path) -> None:
    p = tmp_path / "cli.jsonl"
    cfg = TelemetryConfig(path=p, enabled=True, redact_prompts=True)
    emit(cfg, command="chat", status="ok", duration_ms=1, prompt="secret payload")
    e = _read_lines(p)[0]
    assert "prompt_redacted" in e
    assert "secret payload" not in json.dumps(e)
    assert str(e["prompt_redacted"]).startswith("sha256:")


def test_prompt_visible_when_redaction_off(tmp_path: Path) -> None:
    p = tmp_path / "cli.jsonl"
    cfg = TelemetryConfig(path=p, enabled=True, redact_prompts=False)
    emit(cfg, command="chat", status="ok", duration_ms=1, prompt="visible-payload")
    e = _read_lines(p)[0]
    assert e["prompt_redacted"] == "visible-payload"


def test_file_is_chmod_0600(tmp_path: Path) -> None:
    p = tmp_path / "cli.jsonl"
    cfg = TelemetryConfig(path=p, enabled=True)
    emit(cfg, command="x", status="ok", duration_ms=0)
    mode = p.stat().st_mode & 0o777
    # Mode reflects (0o600 & ~umask). Owner read/write must be set; no group/other write.
    assert mode & 0o600 == 0o600
    assert mode & 0o022 == 0


def test_span_records_duration_and_status(tmp_path: Path) -> None:
    p = tmp_path / "cli.jsonl"
    cfg = TelemetryConfig(path=p, enabled=True)
    with Span(cfg, command="status") as s:
        s.set(provider="claude", route_rule="config.routing.fallback")
    e = _read_lines(p)[0]
    assert e["status"] == "ok"
    assert e["provider"] == "claude"
    assert e["route_rule"] == "config.routing.fallback"
    assert isinstance(e["duration_ms"], int)
    assert e["duration_ms"] >= 0


def test_span_records_error_status_on_exception(tmp_path: Path) -> None:
    p = tmp_path / "cli.jsonl"
    cfg = TelemetryConfig(path=p, enabled=True)
    with pytest.raises(RuntimeError), Span(cfg, command="status"):
        raise RuntimeError("nope")
    e = _read_lines(p)[0]
    assert e["status"] == "error"
    assert e["error"] == "nope"
