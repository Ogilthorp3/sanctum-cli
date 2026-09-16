"""Unit tests for privacy-preserving node macro metrics collection."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from sanctum_cli.mesh.metrics import (
    collect_local_macro_metrics,
    detect_chip,
    detect_country,
    detect_ram_gb,
)
from sanctum_cli.mesh.types import NodeMacroMetrics


def test_detect_country_from_apple_locale() -> None:
    def fake_runner(argv: list[str]) -> str:
        if argv == ["defaults", "read", "-g", "AppleLocale"]:
            return "en_CA"
        raise RuntimeError("unexpected command")

    assert detect_country(fake_runner) == "CA"


def test_detect_country_fallback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_runner(_argv: list[str]) -> str:
        raise RuntimeError("command failed")

    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert detect_country(failing_runner) == "FR"

    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    assert detect_country(failing_runner) == "ZZ"


def test_detect_chip() -> None:
    def fake_runner(argv: list[str]) -> str:
        if argv == ["sysctl", "-n", "machdep.cpu.brand_string"]:
            return "Apple M4 Max"
        raise RuntimeError("unexpected command")

    assert detect_chip(fake_runner) == "Apple M4 Max"


def test_detect_ram_gb() -> None:
    def fake_runner(argv: list[str]) -> str:
        if argv == ["sysctl", "-n", "hw.memsize"]:
            return str(64 * (1024**3))
        raise RuntimeError("unexpected command")

    assert detect_ram_gb(fake_runner) == 64


def test_collect_local_macro_metrics() -> None:
    def fake_runner(argv: list[str]) -> str:
        if "AppleLocale" in argv:
            return "en_CA"
        if "machdep.cpu.brand_string" in argv:
            return "Apple M4 Pro"
        if "hw.memsize" in argv:
            return str(64 * (1024**3))
        return ""

    now = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
    metrics = collect_local_macro_metrics(
        "ed25519:PUBKEY",
        eval_baseline=0.885,
        offline_ratio=0.9,
        champions_seeded=1,
        champions_adopted=2,
        runner=fake_runner,
        now=now,
    )
    assert metrics.node_id == "ed25519:PUBKEY"
    assert metrics.country == "CA"
    assert metrics.chip == "Apple M4 Pro"
    assert metrics.memory_gb == 64
    assert metrics.offline_ratio == 0.9
    assert metrics.eval_baseline == 0.885
    assert metrics.champions_seeded == 1
    assert metrics.champions_adopted == 2
    assert metrics.timestamp == "2026-09-15T12:00:00+00:00"

    # Roundtrip serialization
    data = metrics.to_dict()
    rebuilt = NodeMacroMetrics.from_dict(data)
    assert rebuilt == metrics
