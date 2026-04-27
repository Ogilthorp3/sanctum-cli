"""End-to-end tests for `sanctum chat`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.providers.base import HealthSnapshot, Provider

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


class _FakeProvider(Provider):
    name = "fake"
    capabilities = 0  # type: ignore[assignment]

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def chat(self, _messages, _opts):  # type: ignore[no-untyped-def]
        yield from self._chunks

    def health(self) -> HealthSnapshot:
        return HealthSnapshot(ok=True, latency_ms=1, quota_remaining=None, detail=None)

    def cost(self, _usage):  # type: ignore[no-untyped-def]
        from decimal import Decimal

        return Decimal(0)


def test_chat_streams_to_stdout(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    fake = _FakeProvider(["Hello, ", "world."])
    with patch("sanctum_cli.commands.chat.make_provider", return_value=fake) as mp:
        result = runner.invoke(app, ["chat", "-p", "mlx_local", "hi"])
    assert result.exit_code == 0, result.stdout
    assert "Hello, world." in result.stdout
    mp.assert_called_once()
    args = mp.call_args
    assert args.args[0] == "mlx_local"


def test_chat_unknown_provider_returns_user_error(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    result = runner.invoke(app, ["chat", "-p", "openai", "hi"])
    assert result.exit_code == 1  # USER_ERROR
    combined = result.stdout + (result.stderr or "")
    assert "unknown provider" in combined.lower()


def test_chat_provider_failure_maps_to_provider_error(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))

    class _Boom(_FakeProvider):
        def chat(self, _messages, _opts):  # type: ignore[no-untyped-def]
            raise RuntimeError("upstream blew up")
            yield  # pragma: no cover

    with patch("sanctum_cli.commands.chat.make_provider", return_value=_Boom([])):
        result = runner.invoke(app, ["chat", "-p", "claude", "hi"])
    assert result.exit_code == 2  # PROVIDER_ERROR
    combined = result.stdout + (result.stderr or "")
    assert "claude" in combined.lower() or "provider" in combined.lower()


def test_chat_telemetry_records_route_and_provider(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    tel_path = tmp_path / "cli.jsonl"

    # Override the telemetry path inside config without altering the YAML
    real_load = __import__("sanctum_cli.config", fromlist=["load"]).load

    def patched_load(*args, **kwargs):  # type: ignore[no-untyped-def]
        cfg = real_load(*args, **kwargs)
        cfg.cli.telemetry.path = tel_path
        return cfg

    fake = _FakeProvider(["ok"])
    with (
        patch("sanctum_cli.commands.chat.make_provider", return_value=fake),
        patch("sanctum_cli.commands.chat.config.load", side_effect=patched_load),
    ):
        result = runner.invoke(app, ["chat", "-p", "claude", "ping"])
    assert result.exit_code == 0
    assert tel_path.exists()
    events = [json.loads(line) for line in tel_path.read_text().splitlines() if line]
    assert len(events) == 1
    e = events[0]
    assert e["command"] == "chat"
    assert e["provider"] == "claude"
    assert e["route_rule"] == "flag.provider"
    assert e["status"] == "ok"
    assert "prompt_redacted" in e
    assert str(e["prompt_redacted"]).startswith("sha256:")  # default redaction


def test_chat_reads_from_file(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    f = tmp_path / "p.txt"
    f.write_text("from-file", encoding="utf-8")
    captured: dict[str, object] = {}
    fake = _FakeProvider(["ack"])

    def make(name, _cfg):  # type: ignore[no-untyped-def]
        captured["name"] = name
        return fake

    with patch("sanctum_cli.commands.chat.make_provider", side_effect=make):
        result = runner.invoke(app, ["chat", "-p", "mlx_local", "-f", str(f)])
    assert result.exit_code == 0
    assert "ack" in result.stdout
