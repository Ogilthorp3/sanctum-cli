"""Tests for ``sanctum vision`` — multimodal flow + Gemini routing."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import vision
from sanctum_cli.providers.base import HealthSnapshot, Provider

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


class _GeminiFake(Provider):
    name = "gemini"
    from sanctum_cli.providers.base import Capability as _Cap

    capabilities = _Cap.CHAT | _Cap.VISION | _Cap.STREAMING

    def __init__(self) -> None:
        self.captured_messages = None

    def chat(self, messages, _opts):  # type: ignore[no-untyped-def]
        self.captured_messages = messages
        yield "described"

    def health(self) -> HealthSnapshot:
        return HealthSnapshot(ok=True, latency_ms=1, quota_remaining=None, detail=None)

    def cost(self, _u):  # type: ignore[no-untyped-def]
        from decimal import Decimal

        return Decimal(0)


class _NoVisionProvider(_GeminiFake):
    name = "claude"
    from sanctum_cli.providers.base import Capability as _Cap

    capabilities = _Cap.CHAT | _Cap.STREAMING  # No VISION


def _make_image(tmp_path: Path) -> Path:
    """Tiny 1x1 PNG."""
    p = tmp_path / "a.png"
    p.write_bytes(
        bytes.fromhex(
            "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
            "890000000A49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
        )
    )
    return p


def test_detect_image_mime() -> None:
    from pathlib import Path

    kind, mime = vision._detect(Path("foo.png"))
    assert kind == "image"
    assert mime.startswith("image/")


def test_detect_video_mime() -> None:
    from pathlib import Path

    kind, _ = vision._detect(Path("movie.mp4"))
    assert kind == "video"


def test_detect_unknown_falls_back_to_file() -> None:
    from pathlib import Path

    kind, _ = vision._detect(Path("data.bin"))
    assert kind == "file"


def test_vision_attaches_image_and_routes_to_gemini(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    img = _make_image(tmp_path)
    fake = _GeminiFake()
    captured: dict[str, object] = {}

    def make(name, _cfg):  # type: ignore[no-untyped-def]
        captured["name"] = name
        return fake

    with patch("sanctum_cli.commands.vision.make_provider", side_effect=make):
        result = runner.invoke(app, ["vision", str(img), "describe please"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert captured["name"] == "gemini"
    assert "described" in result.stdout
    # Verify the attachment made it through
    assert fake.captured_messages is not None
    msg = fake.captured_messages[0]
    assert len(msg.attachments) == 1
    assert msg.attachments[0].kind == "image"
    assert msg.attachments[0].mime_type.startswith("image/")


def test_vision_telemetry_records_intent_and_file(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    img = _make_image(tmp_path)
    tel = tmp_path / "cli.jsonl"

    real_load = __import__("sanctum_cli.config", fromlist=["load"]).load

    def patched_load(*args, **kwargs):  # type: ignore[no-untyped-def]
        cfg = real_load(*args, **kwargs)
        cfg.cli.telemetry.path = tel
        return cfg

    fake = _GeminiFake()
    with (
        patch("sanctum_cli.commands.vision.make_provider", return_value=fake),
        patch("sanctum_cli.commands.vision.config.load", side_effect=patched_load),
    ):
        result = runner.invoke(app, ["vision", str(img), "what?"])

    assert result.exit_code == 0
    events = [json.loads(line) for line in tel.read_text().splitlines() if line]
    assert events
    e = events[-1]
    assert e["command"] == "vision"
    assert e["provider"] == "gemini"
    assert e["intent"] == "vision"
    assert "file" in e["extra"]
    assert "mime" in e["extra"]


def test_vision_capability_gate_refuses_non_vision_provider(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    img = _make_image(tmp_path)
    fake = _NoVisionProvider()
    with patch("sanctum_cli.commands.vision.make_provider", return_value=fake):
        result = runner.invoke(app, ["vision", str(img), "hi"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "vision" in combined.lower()
