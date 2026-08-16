from sanctum_cli.modules.manifest import ModuleManifest
from sanctum_cli.ship_gates import (
    GateStatus,
    gate_alert_hygiene,
    gate_docs_demo,
    gate_install_uninstall,
    gate_secrets,
)


def _m(**over):
    base = {
        "module": "m",
        "version": "1.0.0",
        "description": "m",
        "docs": "https://x.invalid",
        "demo": "true",
    }
    base.update(over)
    return ModuleManifest.model_validate(base)


def test_secrets_red_when_missing():
    m = _m(secrets=[{"service": "k", "required": True, "generate": "none"}])
    r = gate_secrets(m, keychain_has=lambda a, s: False, is_default=lambda a, s: False)
    assert r.status is GateStatus.RED


def test_secrets_amber_when_default_looking():
    m = _m(secrets=[{"service": "k", "required": True, "generate": "none"}])
    r = gate_secrets(m, keychain_has=lambda a, s: True, is_default=lambda a, s: True)
    assert r.status is GateStatus.AMBER


def test_alert_hygiene_red_on_dead_sink():
    m = _m(alerts={"sink": "chitti", "pager_conditions": []})
    r = gate_alert_hygiene(m, sink_live=lambda name: False, probe_is_false_green=lambda p: False)
    assert r.status is GateStatus.RED
    assert "sink" in r.detail.lower()


def test_alert_hygiene_red_on_false_green_probe():
    m = _m(probes=["m.p"], alerts={"sink": "chitti", "pager_conditions": []})
    r = gate_alert_hygiene(m, sink_live=lambda n: True, probe_is_false_green=lambda p: p == "m.p")
    assert r.status is GateStatus.RED


def test_docs_demo_green_when_both_present():
    m = _m()
    r = gate_docs_demo(m, docs_resolves=lambda u: True, demo_exits_zero=lambda c: True)
    assert r.status is GateStatus.GREEN


# ── gate_install_uninstall ─────────────────────────────────────────────


def test_install_uninstall_red_when_no_rename_suffix():
    """Empty rename_suffix → RED (no uninstall handler)."""
    m = _m(
        uninstall={
            "rename_suffix": "",
            "bootout_labels": [],
            "revoke_secrets": [],
            "remove_paths": [],
        }
    )
    r = gate_install_uninstall(m)
    assert r.status is GateStatus.RED
    assert "no uninstall handler" in r.detail


def test_install_uninstall_green_with_default_manifest():
    """Default manifest (rename_suffix='.uninstalled-{date}') → GREEN."""
    m = _m()
    r = gate_install_uninstall(m)
    assert r.status is GateStatus.GREEN
    assert "reversible" in r.detail
