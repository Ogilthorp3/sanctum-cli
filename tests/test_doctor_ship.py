"""Tests for sanctum doctor --ship <module> gate evaluator."""

from sanctum_cli.modules.manifest import ModuleManifest
from sanctum_cli.modules.registry import ModuleRegistry
from sanctum_cli.ship_gates import GateStatus
from sanctum_cli.commands.ship import evaluate


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
        "heal_action_ok": lambda l: True,
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
        "heal_action_ok": lambda l: True, "sink_live": lambda n: True,
        "probe_is_false_green": lambda p: False,
        "soak_days": lambda m: 8.0, "soak_clean": lambda m: True,
        "docs_resolves": lambda u: True, "demo_exits_zero": lambda c: True,
    })
    assert res.verdict is GateStatus.GREEN
