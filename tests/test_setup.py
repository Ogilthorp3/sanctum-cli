"""Unit tests for the Setup Assistant seams (``sanctum setup``).

Everything here exercises the pure orchestration — probe aggregation, tier-awareness,
and action dispatch — by patching the seams, so no test touches the network, the real
keychain, or the real instance.yaml.
"""

from __future__ import annotations

import types

import pytest
import yaml

from sanctum_cli.commands import setup

# ── gather_state / tier-awareness ──────────────────────────────────────────────


@pytest.fixture
def _stub_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutral, offline stand-ins for every cheap probe gather_state calls."""
    monkeypatch.setattr(setup, "_version", lambda: "9.9.9")
    monkeypatch.setattr(setup, "_instance_name", lambda: "Test Box")
    monkeypatch.setattr(setup, "_tailscale_installed", lambda: True)
    monkeypatch.setattr(setup, "_oauth_stored", lambda: False)
    monkeypatch.setattr(setup, "_tcc_grant_count", lambda: 17)


def test_state_basic_tier_hides_haus_panes(monkeypatch: pytest.MonkeyPatch, _stub_probes: None) -> None:
    monkeypatch.setattr(setup, "_tier", lambda: "basic")
    state = setup.gather_state()
    assert state["tier"] == "basic"
    # The whole macOS permission wall is haus-only — never shown to a basic install.
    assert "fda" not in state["steps"]
    assert "automation" not in state["steps"]
    assert state["steps"]["oauth"]["status"] == "todo"


def test_state_haus_tier_shows_permission_panes(monkeypatch: pytest.MonkeyPatch, _stub_probes: None) -> None:
    monkeypatch.setattr(setup, "_tier", lambda: "haus")
    state = setup.gather_state()
    assert state["tier"] == "haus"
    assert state["steps"]["fda"]["status"] == "ok"  # 17 >= 13
    assert state["steps"]["fda"]["anchor"] == setup._FDA_ANCHOR
    assert state["steps"]["automation"]["status"] == "unknown"  # honestly undetectable


def test_state_fda_attention_when_under_threshold(
    monkeypatch: pytest.MonkeyPatch, _stub_probes: None
) -> None:
    monkeypatch.setattr(setup, "_tier", lambda: "haus")
    monkeypatch.setattr(setup, "_tcc_grant_count", lambda: 4)
    assert setup.gather_state()["steps"]["fda"]["status"] == "attention"


def test_state_fda_unknown_when_tcc_unreadable(
    monkeypatch: pytest.MonkeyPatch, _stub_probes: None
) -> None:
    monkeypatch.setattr(setup, "_tier", lambda: "haus")
    monkeypatch.setattr(setup, "_tcc_grant_count", lambda: None)  # no FDA to read TCC.db
    assert setup.gather_state()["steps"]["fda"]["status"] == "unknown"


# ── identity action: block-preserving read-modify-write ────────────────────────


def test_identity_save_preserves_other_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    inst = tmp_path / "instance.yaml"  # type: ignore[operator]
    inst.write_text(
        "instance:\n  name: Old Name\n  slug: old-name\n"
        "cli:\n  default_provider: mlx_local\n"
        "notifications:\n  owner_name: Bert\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    result = setup.do_action("identity", "save", {"name": "Casa Verde"})
    assert result["ok"] is True

    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["instance"]["name"] == "Casa Verde"
    assert data["instance"]["slug"] == "casa-verde"
    # The sibling blocks a fresh scaffold/init would have clobbered survive:
    assert data["cli"]["default_provider"] == "mlx_local"
    assert data["notifications"]["owner_name"] == "Bert"
    # A .bak of the pre-edit file was written.
    assert (inst.parent / "instance.yaml.bak").exists()


def test_identity_save_rejects_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "instance.yaml"))  # type: ignore[operator]
    assert setup.do_action("identity", "save", {"name": "   "})["ok"] is False


# ── tailscale creds: verify-before-store ───────────────────────────────────────


def test_creds_missing_input_stores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.commands import tailnet

    called = {"stored": False}
    monkeypatch.setattr(tailnet, "_store_creds", lambda *a, **k: called.__setitem__("stored", True))
    res = setup.do_action("tailscale", "creds", {"client_id": "", "client_secret": ""})
    assert res["ok"] is False
    assert called["stored"] is False


def test_creds_missing_scope_stores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.commands import tailnet

    stored = {"v": False}
    monkeypatch.setattr(tailnet, "_mint_oauth_token", lambda *a, **k: "tok")
    # ACL ok (200) but Devices forbidden (403) → a scope is missing.
    monkeypatch.setattr(
        tailnet,
        "_api_request",
        lambda method, path, token, **k: (200 if path.endswith("/acl") else 403, ""),
    )
    monkeypatch.setattr(tailnet, "_store_creds", lambda *a, **k: stored.__setitem__("v", True))
    res = setup.do_action("tailscale", "creds", {"client_id": "id", "client_secret": "sec"})
    assert res["ok"] is False
    assert "Devices" in res["detail"]
    assert stored["v"] is False  # fail-closed: nothing written


def test_creds_both_scopes_ok_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.commands import tailnet

    stored = {}
    monkeypatch.setattr(tailnet, "_mint_oauth_token", lambda *a, **k: "tok")
    monkeypatch.setattr(tailnet, "_api_request", lambda *a, **k: (200, ""))
    monkeypatch.setattr(
        tailnet, "_store_creds", lambda cid, sec: stored.update(id=cid, secret=sec)
    )
    res = setup.do_action("tailscale", "creds", {"client_id": "id1", "client_secret": "sec1"})
    assert res["ok"] is True
    assert stored == {"id": "id1", "secret": "sec1"}


# ── apply: drives the real command via subprocess ──────────────────────────────


def test_apply_maps_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="✓ APPLIED — live", stderr=""),
    )
    res = setup.do_action("tailscale", "apply", {})
    assert res["ok"] is True
    assert "APPLIED" in res["detail"]

    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=2, stdout="", stderr="boom"),
    )
    assert setup.do_action("tailscale", "apply", {})["ok"] is False


# ── provider: fail-closed store/probe/revoke ───────────────────────────────────


def _patch_provider(monkeypatch: pytest.MonkeyPatch, *, healthy: bool) -> dict[str, object]:
    from sanctum_cli.commands import onboard

    log: dict[str, object] = {"stored": None, "revoked": None, "config": None}
    monkeypatch.setattr(onboard, "_CLAUDE_KEYCHAIN", ("anthropic-api-key", "sanctum"))
    monkeypatch.setattr(onboard, "_GEMINI_KEYCHAIN", ("google-ai-api-key", "sanctum"))
    monkeypatch.setattr(
        onboard, "store_device_secret", lambda **k: log.__setitem__("stored", k["service"])
    )
    monkeypatch.setattr(onboard, "_config_with_provider_overrides", lambda **k: object())
    monkeypatch.setattr(
        onboard, "_provider_health", lambda name, cfg: types.SimpleNamespace(ok=healthy, detail="x")
    )
    monkeypatch.setattr(
        onboard, "_revoke_device_secret", lambda **k: log.__setitem__("revoked", k["service"])
    )
    monkeypatch.setattr(
        onboard, "set_provider_config", lambda **k: log.__setitem__("config", k)
    )
    return log


def test_provider_rejected_key_is_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_provider(monkeypatch, healthy=False)
    res = setup.do_action("provider", "save", {"kind": "claude", "key": "sk-bad"})
    assert res["ok"] is False
    assert log["revoked"] == "anthropic-api-key"  # fail-closed
    assert log["config"] is None  # nothing persisted


def test_provider_good_key_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_provider(monkeypatch, healthy=True)
    res = setup.do_action("provider", "save", {"kind": "gemini", "key": "AIza-good"})
    assert res["ok"] is True
    assert log["stored"] == "google-ai-api-key"
    assert log["revoked"] is None
    assert log["config"] is not None


# ── dispatch + probe routing ───────────────────────────────────────────────────


def test_unknown_action(monkeypatch: pytest.MonkeyPatch) -> None:
    res = setup.do_action("nope", "nope", {})
    assert res["ok"] is False
    assert "unknown action" in res["detail"]


def test_probe_routes_to_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "_probe_verify", lambda: {"tier": "basic", "passed": 3})
    assert setup.probe("verify")["passed"] == 3


def test_probe_unknown_id(monkeypatch: pytest.MonkeyPatch, _stub_probes: None) -> None:
    monkeypatch.setattr(setup, "_tier", lambda: "basic")
    assert setup.probe("does-not-exist")["status"] == "unknown"
