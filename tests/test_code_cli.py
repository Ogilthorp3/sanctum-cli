"""``sanctum code`` is a thin shim — verify it forces Claude routing."""

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


class _Fake(Provider):
    name = "fake"
    capabilities = 0  # type: ignore[assignment]

    def __init__(self) -> None: ...

    def chat(self, _m, _o):  # type: ignore[no-untyped-def]
        yield "ok"

    def health(self) -> HealthSnapshot:
        return HealthSnapshot(ok=True, latency_ms=1, quota_remaining=None, detail=None)

    def cost(self, _u):  # type: ignore[no-untyped-def]
        from decimal import Decimal

        return Decimal(0)


def test_code_routes_to_claude(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    tel = tmp_path / "cli.jsonl"
    real_load = __import__("sanctum_cli.config", fromlist=["load"]).load

    def patched_load(*args, **kwargs):  # type: ignore[no-untyped-def]
        cfg = real_load(*args, **kwargs)
        cfg.cli.telemetry.path = tel
        return cfg

    fake = _Fake()
    captured: dict[str, object] = {}

    def make(name, _cfg):  # type: ignore[no-untyped-def]
        captured["name"] = name
        return fake

    with (
        patch("sanctum_cli.commands.chat.make_provider", side_effect=make),
        patch("sanctum_cli.commands.chat.config.load", side_effect=patched_load),
    ):
        result = runner.invoke(app, ["code", "write a quicksort in python"])

    assert result.exit_code == 0, result.stdout
    assert captured["name"] == "claude"
    events = [json.loads(line) for line in tel.read_text().splitlines() if line]
    assert events
    assert events[-1]["provider"] == "claude"
    assert events[-1]["route_rule"] == "flag.provider"
