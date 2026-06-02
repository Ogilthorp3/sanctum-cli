"""Tests for sanctum doctor --ship <module> gate evaluator."""

import json
from pathlib import Path

import pytest

from sanctum_cli.commands.ship import (  # type: ignore[attr-defined]
    _soak_clean,
    _soak_days,
    evaluate,
)
from sanctum_cli.modules.manifest import ModuleManifest
from sanctum_cli.modules.registry import ModuleRegistry
from sanctum_cli.ship_gates import GateStatus, gate_soak


def _reg() -> ModuleRegistry:
    m = ModuleManifest.model_validate({
        "module": "backup", "version": "1.0.0", "description": "b",
        "secrets": [{"service": "k", "required": True, "generate": "none"}],
        "docs": "https://x.invalid", "demo": "true",
    })
    return ModuleRegistry(manifests={"backup": m})


def test_evaluate_red_when_secret_missing_and_sink_dead() -> None:
    res = evaluate("backup", registry=_reg(), adapters={
        "keychain_has": lambda a, s: False,
        "is_default": lambda a, s: False,
        "heal_action_ok": lambda l: True,  # noqa: E741
        "sink_live": lambda n: False,
        "probe_is_false_green": lambda p: False,
        "soak_days": lambda m: None,
        "soak_clean": lambda m: False,
        "docs_resolves": lambda u: True,
        "demo_exits_zero": lambda c: True,
    })
    assert res.verdict is GateStatus.RED
    names = {g.name for g in res.gates if g.status is GateStatus.RED}
    assert {"secrets-bootstrap", "alert-hygiene", "soak"} <= names


def test_evaluate_green_when_all_pass() -> None:
    res = evaluate("backup", registry=_reg(), adapters={
        "keychain_has": lambda a, s: True, "is_default": lambda a, s: False,
        "heal_action_ok": lambda l: True, "sink_live": lambda n: True,  # noqa: E741
        "probe_is_false_green": lambda p: False,
        "soak_days": lambda m: 8.0, "soak_clean": lambda m: True,
        "docs_resolves": lambda u: True, "demo_exits_zero": lambda c: True,
    })
    assert res.verdict is GateStatus.GREEN


def test_gate_soak_green_from_written_clean_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gate_soak is GREEN when a clean >=7-day soak result file exists.

    This validates the real adapter chain (default_adapters -> classify_soak)
    rather than just the injected-lambda path.
    """
    # Write a clean 7-day soak result to a temp path.
    result_file = tmp_path / "backup.json"
    result_data = {
        "module": "backup",
        "started_at": "2026-05-01T00:00:00Z",
        "last_at": "2026-05-08T12:00:00Z",  # 7.5 days
        "samples": [
            {
                "ts": "2026-05-04T00:00:00Z",
                "pressure_level": 1,
                "swap_used_mb": 100.0,
                "red_probes": [],
                "service_nonzero": [],
            }
        ],
        "faults": [],
    }
    result_file.write_text(json.dumps(result_data))

    # Build a manifest that points soak.result_path at the temp file.
    m = ModuleManifest.model_validate({
        "module": "backup",
        "version": "1.0.0",
        "description": "b",
        "docs": "https://x.invalid",
        "demo": "true",
        "soak": {"min_days": 7, "result_path": str(result_file)},
    })

    # Use the real ship.py adapter functions (which call classify_soak internally).
    result = gate_soak(m, soak_days=_soak_days, soak_clean=_soak_clean)
    assert result.status is GateStatus.GREEN, f"Expected GREEN, got {result.status}: {result.detail}"
