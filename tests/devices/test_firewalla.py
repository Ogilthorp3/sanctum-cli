"""Firewalla provider — mocked bridge HTTP + mocked SSH, no live calls.

The Firewalla provider drives a Firewalla box through TWO transports:

* the **bridge HTTP** API (Bearer-token authed, default ``http://127.0.0.1:1984``)
  for reads, ``/info``, and policy state — the same surface the existing
  ``screen_time`` engine reads through; and
* the **durable SSH key** (``firewalla.ssh_key`` from instance.yaml) for the few
  box-level operations the bridge does not expose.

These unit tests mock BOTH boundaries — the bridge HTTP fetch
(``_fetch_bridge_json``) and the SSH runner (``_ssh_runner``) — so nothing here
touches the live Firewalla (10.0.0.1 / firewalla.local) or opens a socket. The
live read-only smoke is a separate, env-gated test (``SANCTUM_LIVE_FIREWALLA=1``)
that does a single ``get('/info')`` and never mutates.

SAFETY: mutating ops (``set``, the policy ``snapshot``/``rollback`` restore path)
are exercised only against the mocked bridge; the overnight build never fires a
write against live gear. The provider's own ``set`` returns an ``OpResult`` and
is composed behind ``guarded_apply`` (dry-run / guarded_apply rails) at the
intent layer — never auto-fired.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from sanctum_cli.devices.base import Capability, Creds, DeviceError, NetContext

# ── connect: bridge token + ssh key resolution ────────────────────────


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str = "tok",
    ssh_key: str | None = "/tmp/fake-fw-key",
    info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mock the bridge fetch + token + ssh-key resolution for connect().

    Returns a captured dict so a test can assert what reached each seam.
    """
    from sanctum_cli.devices import firewalla as fw

    captured: dict[str, Any] = {"fetched": []}

    def fake_fetch(path: str) -> dict[str, Any] | None:
        captured["fetched"].append(path)
        if path == "/info":
            return info if info is not None else {"box": {"model": "gold"}}
        return None

    monkeypatch.setattr(fw, "_fetch_bridge_json", fake_fetch)
    monkeypatch.setattr(fw, "_read_bridge_token", lambda: token)
    monkeypatch.setattr(fw, "_resolve_ssh_key", lambda: ssh_key)
    return captured


def _connected(
    monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> Any:
    captured = _patch_connect(monkeypatch, **kw)
    from sanctum_cli.devices.firewalla import FirewallaProvider

    p = FirewallaProvider()
    p.connect(Creds(host="firewalla.local", username="pi", secret=None, key_path=None))
    return p, captured


def test_connect_reads_token_and_ssh_key(monkeypatch: pytest.MonkeyPatch) -> None:
    p, captured = _connected(monkeypatch, token="tok-xyz", ssh_key="/tmp/k")
    assert p._token == "tok-xyz"
    assert p._key_path == "/tmp/k"
    # connect probes /info to refine the brand.
    assert "/info" in captured["fetched"]


def test_connect_refines_brand_from_info_model(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _connected(monkeypatch, info={"box": {"model": "goldpro"}})
    assert p.brand == "firewalla-goldpro"


def test_connect_keeps_generic_brand_when_model_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # /info returns no model → brand stays the generic "firewalla".
    p, _ = _connected(monkeypatch, info={"box": {}})
    assert p.brand == "firewalla"


def test_connect_tolerates_unreachable_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """A None /info (bridge down) must not blow up connect — brand stays generic."""
    from sanctum_cli.devices import firewalla as fw

    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda path: None)
    monkeypatch.setattr(fw, "_read_bridge_token", lambda: "tok")
    monkeypatch.setattr(fw, "_resolve_ssh_key", lambda: None)
    p = fw.FirewallaProvider()
    p.connect(Creds(host="firewalla.local", username="pi", secret=None, key_path=None))
    assert p.brand == "firewalla"


def test_connect_none_creds_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect(None) is allowed — the provider self-resolves token + key."""
    _patch_connect(monkeypatch)
    from sanctum_cli.devices.firewalla import FirewallaProvider

    p = FirewallaProvider()
    p.connect(None)  # must not raise — token/key come from the env, not creds
    assert p._token == "tok"


# ── get: bridge reads ─────────────────────────────────────────────────


def test_get_info_returns_json_string(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _connected(monkeypatch, info={"box": {"model": "gold"}})
    out = p.get("/info")
    assert out is not None
    assert "gold" in out  # serialized info payload


def test_get_unknown_path_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import firewalla as fw

    p, _ = _connected(monkeypatch)
    # A path the bridge has no body for → None (best-effort read, not an error).
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda path: None)
    assert p.get("/nope") is None


def test_get_before_connect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices.firewalla import FirewallaProvider

    p = FirewallaProvider()
    with pytest.raises(DeviceError):
        p.get("/info")


# ── set: mutating bridge op behind an OpResult ────────────────────────


def test_set_pauses_policy_via_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import firewalla as fw

    p, _ = _connected(monkeypatch)
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        posts.append((path, body))
        return {"ok": True}

    monkeypatch.setattr(fw, "_post_bridge_json", fake_post)
    res = p.set("/policy/abc/pause", "true")
    assert res.ok is True
    assert posts == [("/policy/abc/pause", {"value": "true"})]


def test_set_records_before_after(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import firewalla as fw

    p, _ = _connected(monkeypatch)
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda path: {"value": "off"})
    monkeypatch.setattr(fw, "_post_bridge_json", lambda path, body: {"ok": True})
    res = p.set("/policy/abc/pause", "on")
    assert res.after == "on"


def test_set_failed_post_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import firewalla as fw

    p, _ = _connected(monkeypatch)
    monkeypatch.setattr(fw, "_post_bridge_json", lambda path, body: None)  # bridge refused
    res = p.set("/policy/abc/pause", "on")
    assert res.ok is False


def test_set_before_connect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices.firewalla import FirewallaProvider

    p = FirewallaProvider()
    with pytest.raises(DeviceError):
        p.set("/policy/abc/pause", "on")


# ── capabilities ──────────────────────────────────────────────────────


def test_capabilities_advertise_firewalla_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, _ = _connected(monkeypatch)
    caps = p.capabilities()
    assert caps == {
        Capability.READ,
        Capability.POLICY,
        Capability.SCREEN_TIME,
        Capability.WAN_MODE,
    }


def test_capability_op_none_for_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """No brand-specific (path, engaged) binding is exposed (intents drive via set)."""
    p, _ = _connected(monkeypatch)
    assert p.capability_op(Capability.BRIDGE_MODE) is None


# ── snapshot / rollback: policy state ─────────────────────────────────


def test_snapshot_captures_policy_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import firewalla as fw

    policies = {"policies": [{"pid": "1", "paused": False}], "count": 1}
    p, _ = _connected(monkeypatch)
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda path: policies)
    snap = p.snapshot()
    assert snap.brand.startswith("firewalla")
    assert snap.taken_at  # ISO-8601 stamp
    assert "/policies" in snap.data  # captured the policy subtree


def test_snapshot_empty_when_policies_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanctum_cli.devices import firewalla as fw

    p, _ = _connected(monkeypatch)
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda path: None)
    snap = p.snapshot()
    assert snap.data == {}


def test_rollback_restores_captured_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanctum_cli.devices import firewalla as fw
    from sanctum_cli.devices.base import Snapshot

    p, _ = _connected(monkeypatch)
    posts: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        fw, "_post_bridge_json", lambda path, body: posts.append((path, body)) or {"ok": True}
    )
    snap = Snapshot(
        brand="firewalla",
        taken_at="t",
        data={"/policies": '{"policies": [{"pid": "1", "paused": false}]}'},
    )
    res = p.rollback(snap)
    assert res.ok is True
    # rollback re-applies the captured policy state through the bridge.
    assert any(path == "/policies/restore" for path, _ in posts)


def test_rollback_empty_snapshot_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-baseline rollback must report ok=False, not a silent success."""
    from sanctum_cli.devices.base import Snapshot

    p, _ = _connected(monkeypatch)
    res = p.rollback(Snapshot(brand="firewalla", taken_at="t", data={}))
    assert res.ok is False


# ── detect: bridge /info OR socket firewalla.local:22 ─────────────────


def test_detect_one_when_bridge_info_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanctum_cli.devices import firewalla as fw

    # Bridge /info answers → high confidence, no socket probe needed.
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda path: {"box": {"model": "gold"}})
    monkeypatch.setattr(fw, "_ssh_port_open", lambda: False)
    net = NetContext(gateway_ip="10.0.0.1", runner=None)
    assert fw.FirewallaProvider.detect(net) == 1.0


def test_detect_partial_when_only_ssh_port_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanctum_cli.devices import firewalla as fw

    # Bridge unreachable but firewalla.local:22 answers → partial confidence.
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda path: None)
    monkeypatch.setattr(fw, "_ssh_port_open", lambda: True)
    net = NetContext(gateway_ip="10.0.0.1", runner=None)
    score = fw.FirewallaProvider.detect(net)
    assert 0.0 < score < 1.0


def test_detect_zero_when_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import firewalla as fw

    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda path: None)
    monkeypatch.setattr(fw, "_ssh_port_open", lambda: False)
    net = NetContext(gateway_ip="10.0.0.1", runner=None)
    assert fw.FirewallaProvider.detect(net) == 0.0


# ── disconnect ────────────────────────────────────────────────────────


def test_disconnect_is_idempotent_and_safe_unconnected() -> None:
    from sanctum_cli.devices.firewalla import FirewallaProvider

    p = FirewallaProvider()
    p.disconnect()  # never connected: must not raise
    p.disconnect()  # twice: still fine


# ── registry ──────────────────────────────────────────────────────────


def test_provider_registered_under_firewalla() -> None:
    from sanctum_cli.devices import firewalla, registry

    assert "firewalla" in registry._REGISTRY
    assert firewalla.FirewallaProvider in registry._REGISTRY["firewalla"]


def test_satisfies_device_provider_protocol() -> None:
    from sanctum_cli.devices.base import DeviceProvider
    from sanctum_cli.devices.firewalla import FirewallaProvider

    p: DeviceProvider = FirewallaProvider()
    assert isinstance(p, DeviceProvider)  # runtime_checkable structural conformance


# ── live read-only smoke (env-gated, default-skipped) ─────────────────

LIVE_FW = os.environ.get("SANCTUM_LIVE_FIREWALLA") == "1"


@pytest.mark.skipif(
    not LIVE_FW,
    reason=(
        "live Firewalla smoke is opt-in: set SANCTUM_LIVE_FIREWALLA=1 to run "
        "(read-only get /info, no mutation)"
    ),
)
def test_live_firewalla_read_only_info() -> None:
    """Read-only smoke against the REAL Firewalla bridge — opt-in, never mutates.

    Connects with the on-disk bridge token + ssh key and reads ``/info``,
    asserting a non-empty payload. NO ``set`` / pause / policy mutation is
    performed. Skipped unless ``SANCTUM_LIVE_FIREWALLA=1`` so the default gate
    (CI / overnight build / clean checkout) never opens a socket to live gear.
    """
    from sanctum_cli.devices.firewalla import FirewallaProvider

    p = FirewallaProvider()
    try:
        p.connect(Creds(host="firewalla.local", username="pi", secret=None, key_path=None))
        info = p.get("/info")
    finally:
        p.disconnect()
    assert info, "live Firewalla returned an empty /info"
