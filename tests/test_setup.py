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
from sanctum_cli.commands.setup_page import PAGE

# ── gather_state / tier-awareness ──────────────────────────────────────────────


@pytest.fixture
def _stub_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutral, offline stand-ins for every cheap probe gather_state calls."""
    monkeypatch.setattr(setup, "_version", lambda: "9.9.9")
    monkeypatch.setattr(setup, "_instance_name", lambda: "Test Box")
    monkeypatch.setattr(setup, "_tailscale_installed", lambda: True)
    monkeypatch.setattr(setup, "_oauth_stored", lambda: False)
    monkeypatch.setattr(setup, "_tcc_grant_count", lambda: 17)
    monkeypatch.setattr(setup, "_restic_installed", lambda: True)
    monkeypatch.setattr(
        setup,
        "_backup_state",
        lambda: {
            "repo": "r2:sanctum/backup",
            "secondary": None,
            "recipes": ["family", "operator"],
            "default_recipe": "family",
            "keep_daily": 7,
            "keep_weekly": 4,
            "keep_monthly": 12,
        },
    )
    monkeypatch.setattr(
        setup,
        "_family_members",
        lambda: [
            {
                "id": "kid",
                "name": "Kid",
                "role": "child",
                "devices": [{"name": "iPhone", "mac": "AA:BB:CC:00:00:01"}],
            }
        ],
    )
    monkeypatch.setattr(setup, "_firewalla_bridge_url", lambda: "http://127.0.0.1:1984")
    monkeypatch.setattr(setup, "_firewalla_token_stored", lambda: True)
    monkeypatch.setattr(setup, "_firewalla_paired", lambda: True)
    monkeypatch.setattr(setup, "_firewalla_device", lambda: ("", ""))
    monkeypatch.setattr(setup, "_ha_green_url", lambda: "http://homeassistant.local:8123")
    monkeypatch.setattr(setup, "_ha_token_stored", lambda: False)
    monkeypatch.setattr(setup, "_ha_paired", lambda: False)


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


# ── pane tier-gating: backup/network are universal, the haus stack is haus-only ─


def test_state_basic_tier_gets_backup_and_network_only(
    monkeypatch: pytest.MonkeyPatch, _stub_probes: None
) -> None:
    monkeypatch.setattr(setup, "_tier", lambda: "basic")
    steps = setup.gather_state()["steps"]
    assert steps["backup"]["status"] == "ok"  # restic + repo configured (stubs)
    assert steps["network"]["status"] == "unknown"  # scan is on-demand
    # The haus stack never renders for a friend/family install:
    assert "family" not in steps
    assert "firewalla" not in steps
    assert "ha" not in steps


def test_state_haus_tier_gets_the_full_stack(
    monkeypatch: pytest.MonkeyPatch, _stub_probes: None
) -> None:
    monkeypatch.setattr(setup, "_tier", lambda: "haus")
    steps = setup.gather_state()["steps"]
    assert steps["family"]["status"] == "ok"
    assert steps["family"]["members"][0]["devices"][0]["mac"] == "AA:BB:CC:00:00:01"
    assert steps["firewalla"]["status"] == "ok"  # paired + token (stubs)
    assert steps["firewalla"]["url"] == "http://127.0.0.1:1984"
    assert steps["ha"]["status"] == "todo"  # no token, not paired (stubs)
    assert steps["ha"]["token_stored"] is False


def test_state_backup_prefills_policy_for_idempotent_reruns(
    monkeypatch: pytest.MonkeyPatch, _stub_probes: None
) -> None:
    monkeypatch.setattr(setup, "_tier", lambda: "basic")
    b = setup.gather_state()["steps"]["backup"]
    assert b["recipes"] == ["family", "operator"]
    assert b["default_recipe"] == "family"
    assert (b["keep_daily"], b["keep_weekly"], b["keep_monthly"]) == (7, 4, 12)


def test_state_backup_statuses(monkeypatch: pytest.MonkeyPatch, _stub_probes: None) -> None:
    monkeypatch.setattr(setup, "_tier", lambda: "basic")
    monkeypatch.setattr(setup, "_restic_installed", lambda: False)
    assert setup.gather_state()["steps"]["backup"]["status"] == "attention"  # repo, no restic
    base = {
        "repo": None, "secondary": None, "recipes": [], "default_recipe": None,
        "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12,
    }
    monkeypatch.setattr(setup, "_backup_state", lambda: dict(base))
    assert setup.gather_state()["steps"]["backup"]["status"] == "todo"


# ── backup action: policy RMW preserves sibling blocks ─────────────────────────


def test_backup_save_writes_policy_preserving_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    inst = tmp_path / "instance.yaml"  # type: ignore[operator]
    inst.write_text(
        "instance:\n  name: Test Box\n  slug: test-box\n"
        "cli:\n  default_provider: mlx_local\n"
        "services:\n  ha_green:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    result = setup.do_action(
        "backup", "save",
        {"default_recipe": "operator", "keep_daily": 5, "keep_weekly": 2, "keep_monthly": 6},
    )
    assert result["ok"] is True

    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    assert data["cli"]["default_recipe"] == "operator"
    assert data["cli"]["cloud_backup"]["retention"] == {
        "keep_daily": 5, "keep_weekly": 2, "keep_monthly": 6,
    }
    # Sibling blocks survive the RMW:
    assert data["cli"]["default_provider"] == "mlx_local"
    assert data["services"]["ha_green"]["enabled"] is True
    assert (inst.parent / "instance.yaml.bak").exists()


def test_backup_save_rejects_unknown_recipe_and_bad_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    inst = tmp_path / "instance.yaml"  # type: ignore[operator]
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    assert setup.do_action("backup", "save", {"default_recipe": "nope"})["ok"] is False
    assert setup.do_action("backup", "save", {"keep_daily": 0})["ok"] is False
    assert setup.do_action("backup", "save", {"keep_daily": "many"})["ok"] is False
    assert not inst.exists()  # every rejection wrote nothing


# ── family action: devices.yaml RMW preserves the screen-time contract ─────────


def test_family_save_updates_and_preserves_unmodeled_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    devices = tmp_path / "devices.yaml"  # type: ignore[operator]
    devices.write_text(
        "family:\n"
        "  albert:\n"
        "    name: Albert\n"
        "    role: child\n"
        "    curfew: {weekday: '21:00', weekend: '22:00'}\n"
        "    enforce_personal: macpause\n"
        "    personal_devices:\n"
        "      - {name: Old iPhone, mac: 'AA:BB:CC:00:00:01', enforce: macpause}\n"
        "  parent1:\n"
        "    name: Bert\n"
        "    role: parent\n"
        "shared_devices:\n"
        "  - {key: tv, name: Basement TV, hard_curfew: '23:00'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))

    result = setup.do_action(
        "family", "save",
        {
            "members": [
                {
                    "id": "albert",
                    "name": "Albert",
                    "role": "child",
                    "devices": [
                        {"name": "Albert iPhone", "mac": "aa:bb:cc:00:00:01"},
                        {"name": "iPad", "mac": "AA:BB:CC:00:00:02"},
                    ],
                },
                {"name": "Mamie", "role": "parent", "devices": []},
            ]
        },
    )
    assert result["ok"] is True

    data = yaml.safe_load(devices.read_text(encoding="utf-8"))
    albert = data["family"]["albert"]
    # Renamed device kept its per-device enforce flag (matched by MAC, case-folded):
    assert albert["personal_devices"][0] == {
        "name": "Albert iPhone", "mac": "AA:BB:CC:00:00:01", "enforce": "macpause",
    }
    assert albert["personal_devices"][1] == {"name": "iPad", "mac": "AA:BB:CC:00:00:02"}
    # Unmodeled member keys survive:
    assert albert["curfew"] == {"weekday": "21:00", "weekend": "22:00"}
    assert albert["enforce_personal"] == "macpause"
    # Members absent from the form are preserved, never deleted:
    assert data["family"]["parent1"]["name"] == "Bert"
    # A new member lands under its slug; sibling top-level blocks survive:
    assert data["family"]["mamie"]["role"] == "parent"
    assert data["shared_devices"][0]["hard_curfew"] == "23:00"
    assert (devices.parent / "devices.yaml.bak").exists()


def test_family_save_rejects_bad_mac_writing_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    devices = tmp_path / "devices.yaml"  # type: ignore[operator]
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))
    result = setup.do_action(
        "family", "save",
        {"members": [{"name": "Kid", "devices": [{"name": "iPhone", "mac": "not-a-mac"}]}]},
    )
    assert result["ok"] is False
    assert "AA:BB:CC:DD:EE:FF" in result["detail"]
    assert not devices.exists()


def test_family_save_requires_a_member(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(tmp_path / "devices.yaml"))  # type: ignore[operator]
    assert setup.do_action("family", "save", {"members": []})["ok"] is False


# ── firewalla action: verify-before-store, fail-closed ─────────────────────────


def _patch_firewalla(
    monkeypatch: pytest.MonkeyPatch, *, ok: bool, stored_token: str | None = None
) -> dict[str, object]:
    from sanctum_cli.commands import onboard, screen_time
    from sanctum_cli.devices import firewalla

    log: dict[str, object] = {"persisted": None, "probed": None}
    monkeypatch.setattr(firewalla, "_bridge_url", lambda: "http://127.0.0.1:1984")
    monkeypatch.setattr(firewalla, "_read_bridge_token", lambda: stored_token)
    monkeypatch.setattr(
        screen_time,
        "validate_firewalla_pairing",
        lambda url, token, **k: (
            log.__setitem__("probed", (url, token))
            or screen_time.PairingResult(
                "paired" if ok else "auth_rejected", ok, "bridge sees 3 device(s)" if ok else "bad token"
            )
        ),
    )
    monkeypatch.setattr(
        onboard, "set_firewalla_bridge", lambda **kw: log.__setitem__("persisted", kw)
    )
    return log


def test_firewalla_save_rejected_token_persists_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _patch_firewalla(monkeypatch, ok=False)
    res = setup.do_action("firewalla", "save", {"url": "http://127.0.0.1:1984", "token": "bad"})
    assert res["ok"] is False
    assert log["persisted"] is None  # fail-closed: nothing written


def test_firewalla_save_pairs_on_authenticated_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_firewalla(monkeypatch, ok=True)
    res = setup.do_action(
        "firewalla", "save",
        {"url": "http://192.168.1.2:8834", "token": "tok1", "device_ip": "192.168.1.1"},
    )
    assert res["ok"] is True
    persisted = log["persisted"]
    assert persisted["token"] == "tok1"  # type: ignore[index]
    assert persisted["port"] == 8834  # type: ignore[index]
    assert persisted["device_ip"] == "192.168.1.1"  # type: ignore[index]


def test_firewalla_save_blank_token_reuses_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_firewalla(monkeypatch, ok=True, stored_token="stored-tok")
    res = setup.do_action("firewalla", "save", {"url": "", "token": ""})
    assert res["ok"] is True
    assert log["probed"] == ("http://127.0.0.1:1984", "stored-tok")  # env-default URL + disk token


def test_firewalla_save_no_token_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_firewalla(monkeypatch, ok=True, stored_token=None)
    res = setup.do_action("firewalla", "save", {"token": "   "})
    assert res["ok"] is False
    assert log["probed"] is None  # no unauthenticated probe was sent
    assert log["persisted"] is None


# ── ha action: verify-before-store against the live "API running." marker ─────


def _patch_ha(
    monkeypatch: pytest.MonkeyPatch, *, running: bool, stored_token: str | None = None
) -> dict[str, object]:
    from sanctum_cli.commands import onboard
    from sanctum_cli.devices import ha_green

    log: dict[str, object] = {"persisted": None, "checked": None}
    monkeypatch.setattr(ha_green, "_ha_url", lambda: "http://homeassistant.local:8123")
    monkeypatch.setattr(ha_green, "_read_ha_token", lambda: stored_token)
    monkeypatch.setattr(
        ha_green,
        "api_running",
        lambda *, url=None, token=None: log.__setitem__("checked", (url, token)) or running,
    )
    monkeypatch.setattr(ha_green, "ha_version", lambda *, url=None, token=None: "2026.7.2")
    monkeypatch.setattr(onboard, "set_ha_green", lambda **kw: log.__setitem__("persisted", kw))
    return log


def test_ha_save_unverified_persists_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_ha(monkeypatch, running=False)
    res = setup.do_action("ha", "save", {"url": "http://ha.local:8123", "token": "t"})
    assert res["ok"] is False
    assert log["persisted"] is None


def test_ha_save_verifies_then_persists_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_ha(monkeypatch, running=True)
    res = setup.do_action("ha", "save", {"url": "http://ha.example:8123", "token": "tok9"})
    assert res["ok"] is True
    persisted = log["persisted"]
    assert persisted["token"] == "tok9"  # type: ignore[index]
    assert persisted["host"] == "ha.example"  # type: ignore[index]
    assert persisted["port"] == 8123  # type: ignore[index]
    assert "2026.7.2" in res["detail"]


def test_ha_save_blank_token_reverifies_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_ha(monkeypatch, running=True, stored_token="disk-tok")
    res = setup.do_action("ha", "save", {"url": "", "token": ""})
    assert res["ok"] is True
    # A blank form token means "the one on disk" — api_running got token=None (resolver path)
    assert log["checked"][1] is None  # type: ignore[index]
    assert log["persisted"]["token"] is None  # type: ignore[index]


def test_ha_save_no_token_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _patch_ha(monkeypatch, running=True, stored_token=None)
    res = setup.do_action("ha", "save", {"token": ""})
    assert res["ok"] is False
    assert log["checked"] is None  # no probe without any token
    assert log["persisted"] is None


# ── probe routing for the new panes ────────────────────────────────────────────


def test_probe_routes_new_pane_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "_probe_network", lambda: {"nat": "single"})
    monkeypatch.setattr(setup, "_probe_firewalla", lambda: {"state": "paired"})
    monkeypatch.setattr(setup, "_probe_ha", lambda: {"api_running": True})
    assert setup.probe("network")["nat"] == "single"
    assert setup.probe("firewalla")["state"] == "paired"
    assert setup.probe("ha")["api_running"] is True


def test_probe_firewalla_without_token_never_hits_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanctum_cli.commands import screen_time
    from sanctum_cli.devices import firewalla

    monkeypatch.setattr(firewalla, "_read_bridge_token", lambda: None)

    def _boom(*a: object, **k: object) -> object:  # pragma: no cover - must not run
        raise AssertionError("probe must not run without a token")

    monkeypatch.setattr(screen_time, "validate_firewalla_pairing", _boom)
    res = setup.probe("firewalla")
    assert res["state"] == "no_token"
    assert res["ok"] is False


# ── the page ships the new panes (static contract, no browser) ─────────────────


def test_page_declares_new_panes_and_tier_gate() -> None:
    for pane_id in ("'backup'", "'family'", "'network'", "'firewalla'", "'ha'"):
        assert f"id:{pane_id}" in PAGE
    # The haus stack is client-gated on the SERVER-reported tier:
    assert "STATE.tier==='haus'" in PAGE
    # Every pane's apply step drives the wizard's action contract:
    for action in (
        "act('backup','save'",
        "act('family','save'",
        "act('firewalla','save'",
        "act('ha','save'",
    ):
        assert action in PAGE
    # And the on-demand probes are wired:
    for probe_id in ("probe('network')", "probe('firewalla')", "probe('ha')"):
        assert probe_id in PAGE


def test_page_greets_as_the_wizard() -> None:
    """Bert's Burning Man name is on the door — title + one welcome-pane line."""
    assert "<title>Sanctum Setup — the Wizard is in.</title>" in PAGE
    assert "The Wizard will see you now." in PAGE
