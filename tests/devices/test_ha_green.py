"""HA Green provider — mocked REST HTTP, no live calls.

The HA Green provider drives a Home Assistant Green (HAOS appliance at the static
LAN reservation ``10.0.0.3:8123``) through a single **Bearer-(owner-)token REST**
transport — exactly the shape of the Firewalla bridge, so these tests mirror
``tests/devices/test_firewalla.py``. They mock the REST boundary
(``_fetch_api_json`` fail-soft, plus the REAL strict ``get`` driven through an
``httpx.MockTransport``) and the token resolver, so nothing here touches the live
Green or opens a socket. The live read-only smoke is a separate, env-gated test
(``SANCTUM_LIVE_HA_GREEN=1``) that does a single ``get('/api/')`` and never mutates.

The provider is READ-ONLY in sanctum-cli (HA state mutations ride the toolkit's
WebSocket path), so ``set``/``snapshot``/``rollback`` are honest no-ops and the
provider advertises only ``Capability.READ``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from sanctum_cli.devices.base import Capability, Creds, DeviceError, NetContext

# ── connect: owner-token resolution + brand refine ────────────────────


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str | None = "tok",
    config_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mock the REST fetch + token resolution for connect().

    Returns a captured dict so a test can assert which paths reached the seam.
    """
    from sanctum_cli.devices import ha_green as hg

    captured: dict[str, Any] = {"fetched": []}

    def fake_fetch(path: str, *, url: str | None = None, token: str | None = None) -> Any:
        captured["fetched"].append(path)
        if path == "/api/config":
            return config_body if config_body is not None else {"version": "2026.6.1"}
        if path == "/api/":
            return {"message": "API running."}
        return None

    monkeypatch.setattr(hg, "_fetch_api_json", fake_fetch)
    monkeypatch.setattr(hg, "_read_ha_token", lambda: token)
    return captured


def _connected(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    captured = _patch_connect(monkeypatch, **kw)
    from sanctum_cli.devices.ha_green import HaGreenProvider

    p = HaGreenProvider()
    p.connect(Creds(host="10.0.0.3", username="owner", secret=None, key_path=None))
    return p, captured


def test_connect_reads_token(monkeypatch: pytest.MonkeyPatch) -> None:
    p, captured = _connected(monkeypatch, token="tok-xyz")
    assert p._token == "tok-xyz"
    # connect probes /api/config to refine the brand.
    assert "/api/config" in captured["fetched"]


def test_connect_refines_brand_from_config_version(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _connected(monkeypatch, config_body={"version": "2026.6.3"})
    assert p.brand == "ha-green-2026.6.3"


def test_connect_keeps_generic_brand_when_version_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # /api/config returns no version → brand stays the generic "ha-green".
    p, _ = _connected(monkeypatch, config_body={})
    assert p.brand == "ha-green"


def test_connect_tolerates_unreachable_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A None /api/config (HA down) must not blow up connect — brand stays generic."""
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(hg, "_fetch_api_json", lambda path, **kw: None)
    monkeypatch.setattr(hg, "_read_ha_token", lambda: "tok")
    p = hg.HaGreenProvider()
    p.connect(Creds(host="10.0.0.3", username="owner", secret=None, key_path=None))
    assert p.brand == "ha-green"


def test_connect_none_creds_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect(None) is allowed — the provider self-resolves the owner token."""
    _patch_connect(monkeypatch)
    from sanctum_cli.devices.ha_green import HaGreenProvider

    p = HaGreenProvider()
    p.connect(None)  # must not raise — token comes from the env/on-disk, not creds
    assert p._token == "tok"


# ── get: strict REST reads (real httpx.MockTransport) ─────────────────


def _install_strict_transport(
    monkeypatch: pytest.MonkeyPatch, *, status: int | None, body: object
) -> None:
    """Drive the REAL strict GET seam through an httpx.MockTransport (no socket).

    Mirrors the Firewalla suite's pattern: intercept only at the socket layer so
    httpx's real URL-construction / status / JSON parsing runs — the cross-layer
    contract is proven against the actual transport, not a stub of the seam
    (CLAUDE.md "Don't mock cheap subprocess boundaries"). ``status=None`` simulates
    an unreachable Green (ConnectError).
    """
    import json as _json

    import httpx

    from sanctum_cli.devices import ha_green as hg

    def handler(request: httpx.Request) -> httpx.Response:
        if status is None:
            raise httpx.ConnectError("simulated unreachable")
        content = _json.dumps(body).encode() if body is not None else b"not json"
        return httpx.Response(status, request=request, content=content)

    monkeypatch.setattr(hg, "_ha_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(hg, "_read_ha_token", lambda: "tok")


def test_get_api_returns_json_string(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _connected(monkeypatch)
    _install_strict_transport(monkeypatch, status=200, body={"message": "API running."})
    out = p.get("/api/")
    assert out is not None
    assert "API running." in out  # serialized payload


def test_get_404_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine path-unknown (404) is best-effort None — the contract's "no body"."""
    p, _ = _connected(monkeypatch)
    _install_strict_transport(monkeypatch, status=404, body={"error": "not found"})
    assert p.get("/api/nope") is None


def test_get_before_connect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices.ha_green import HaGreenProvider

    p = HaGreenProvider()
    with pytest.raises(DeviceError):
        p.get("/api/")


def test_get_transport_down_raises_device_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A powered-off Green (transport error) must RAISE DeviceError, not return None."""
    p, _ = _connected(monkeypatch)
    _install_strict_transport(monkeypatch, status=None, body=None)  # ConnectError
    with pytest.raises(DeviceError):
        p.get("/api/")


def test_get_auth_reject_raises_device_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token-reject (401) must RAISE DeviceError — the Supervisor-proxy access model.

    The owner token is accepted on REST ``/api/*`` but REJECTED (401) on the
    ``/api/hassio/*`` Supervisor proxy. A strict read of such a path must surface
    that honestly, not disguise it as 'up, empty body'.
    """
    p, _ = _connected(monkeypatch)
    _install_strict_transport(monkeypatch, status=401, body={"error": "unauthorized"})
    with pytest.raises(DeviceError):
        p.get("/api/hassio/supervisor/info")


def test_get_server_error_raises_device_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any other non-200 (e.g. 503) is a transport-class failure → DeviceError."""
    p, _ = _connected(monkeypatch)
    _install_strict_transport(monkeypatch, status=503, body={"error": "down"})
    with pytest.raises(DeviceError):
        p.get("/api/")


def test_get_missing_token_raises_device_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No token at all is an auth failure → DeviceError, not a silent None."""
    from sanctum_cli.devices import ha_green as hg

    p, _ = _connected(monkeypatch)
    monkeypatch.setattr(hg, "_read_ha_token", lambda: None)
    with pytest.raises(DeviceError):
        p.get("/api/")


def test_get_200_non_dict_body_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable, authorized box that answers 200 with a non-dict body → None."""
    p, _ = _connected(monkeypatch)
    _install_strict_transport(monkeypatch, status=200, body=[1, 2, 3])  # JSON list, not dict
    assert p.get("/api/") is None


# ── api_running / ha_version: the honest-verify primitives ────────────


def test_api_running_true_on_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(hg, "_fetch_api_json", lambda path, **kw: {"message": "API running."})
    assert hg.api_running() is True


def test_api_running_false_on_wrong_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 whose body is NOT the running marker is honestly 'not running'."""
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(hg, "_fetch_api_json", lambda path, **kw: {"message": "something else"})
    assert hg.api_running() is False


def test_api_running_false_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(hg, "_fetch_api_json", lambda path, **kw: None)
    assert hg.api_running() is False


def test_api_running_threads_explicit_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A just-entered token (onboard gate) is threaded through to the fetch seam."""
    from sanctum_cli.devices import ha_green as hg

    seen: dict[str, Any] = {}

    def fake_fetch(path: str, *, url: str | None = None, token: str | None = None) -> Any:
        seen["token"] = token
        return {"message": "API running."}

    monkeypatch.setattr(hg, "_fetch_api_json", fake_fetch)
    assert hg.api_running(token="just-entered") is True
    assert seen["token"] == "just-entered"


def test_ha_version_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(hg, "_fetch_api_json", lambda path, **kw: {"version": "2026.6.1"})
    assert hg.ha_version() == "2026.6.1"


def test_ha_version_none_when_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(hg, "_fetch_api_json", lambda path, **kw: None)
    assert hg.ha_version() is None


# ── tailscale node presence (pure parser + fail-soft seam) ────────────


def test_parse_tailscale_node_matches_exact_hostname_column() -> None:
    from sanctum_cli.devices import ha_green as hg

    text = (
        "100.64.0.1   bert-mbp        bert@   macOS   -\n"
        "100.64.0.9   homeassistant   bert@   linux   active; direct\n"
    )
    assert hg._parse_tailscale_node(text, "homeassistant") is True
    # Substring lookalikes must NOT match (exact column match only).
    assert hg._parse_tailscale_node(text, "home") is False
    assert hg._parse_tailscale_node("", "homeassistant") is False


def test_tailscale_node_present_uses_status_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(
        hg, "_tailscale_status_text", lambda: "100.64.0.9 homeassistant bert@ linux -"
    )
    assert hg.tailscale_node_present() is True
    monkeypatch.setattr(hg, "_tailscale_status_text", lambda: "")  # no tailscale / not joined
    assert hg.tailscale_node_present() is False


# ── detect: /api/ running marker OR TCP port open ─────────────────────


def test_detect_one_when_api_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(hg, "_fetch_api_json", lambda path, **kw: {"message": "API running."})
    monkeypatch.setattr(hg, "_port_open", lambda: False)
    net = NetContext(gateway_ip="10.0.0.1", runner=None)
    assert hg.HaGreenProvider.detect(net) == 1.0


def test_detect_partial_when_only_port_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(hg, "_fetch_api_json", lambda path, **kw: None)  # API down / no token
    monkeypatch.setattr(hg, "_port_open", lambda: True)
    net = NetContext(gateway_ip="10.0.0.1", runner=None)
    score = hg.HaGreenProvider.detect(net)
    assert 0.0 < score < 1.0


def test_detect_zero_when_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import ha_green as hg

    monkeypatch.setattr(hg, "_fetch_api_json", lambda path, **kw: None)
    monkeypatch.setattr(hg, "_port_open", lambda: False)
    net = NetContext(gateway_ip="10.0.0.1", runner=None)
    assert hg.HaGreenProvider.detect(net) == 0.0


# ── read-only surface: set / snapshot / rollback are honest no-ops ────


def test_set_is_refused_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _connected(monkeypatch)
    res = p.set("/api/states/light.kitchen", "on")
    assert res.ok is False  # read-only surface — never mutates, never raises


def test_set_before_connect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices.ha_green import HaGreenProvider

    p = HaGreenProvider()
    with pytest.raises(DeviceError):
        p.set("/api/states/x", "on")


def test_snapshot_is_empty_and_rollback_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only surface captures nothing; an empty rollback is honestly ok=False."""
    p, _ = _connected(monkeypatch)
    snap = p.snapshot()
    assert snap.data == {}
    assert snap.brand.startswith("ha-green")
    assert snap.taken_at  # ISO-8601 stamp
    res = p.rollback(snap)
    assert res.ok is False  # never a silent "restored" over nothing


# ── capabilities ──────────────────────────────────────────────────────


def test_capabilities_are_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _connected(monkeypatch)
    assert p.capabilities() == {Capability.READ}


def test_capability_op_none(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = _connected(monkeypatch)
    assert p.capability_op(Capability.BRIDGE_MODE) is None


# ── disconnect ────────────────────────────────────────────────────────


def test_disconnect_is_idempotent_and_safe_unconnected() -> None:
    from sanctum_cli.devices.ha_green import HaGreenProvider

    p = HaGreenProvider()
    p.disconnect()  # never connected: must not raise
    p.disconnect()  # twice: still fine


# ── registry / protocol ───────────────────────────────────────────────


def test_provider_registered_under_ha_green() -> None:
    from sanctum_cli.devices import ha_green, registry

    assert "ha-green" in registry._REGISTRY
    assert ha_green.HaGreenProvider in registry._REGISTRY["ha-green"]


def test_satisfies_device_provider_protocol() -> None:
    from sanctum_cli.devices.base import DeviceProvider
    from sanctum_cli.devices.ha_green import HaGreenProvider

    p: DeviceProvider = HaGreenProvider()
    assert isinstance(p, DeviceProvider)  # runtime_checkable structural conformance


# ── live read-only smoke (env-gated, default-skipped) ─────────────────

LIVE_HA = os.environ.get("SANCTUM_LIVE_HA_GREEN") == "1"


@pytest.mark.skipif(
    not LIVE_HA,
    reason=(
        "live HA Green smoke is opt-in: set SANCTUM_LIVE_HA_GREEN=1 to run "
        "(read-only get /api/, no mutation)"
    ),
)
def test_live_ha_green_read_only_api() -> None:
    """Read-only smoke against the REAL HA Green — opt-in, never mutates.

    Connects with the on-disk owner token and reads ``/api/``, asserting the
    running marker. NO ``set`` / state mutation is performed. Skipped unless
    ``SANCTUM_LIVE_HA_GREEN=1`` so the default gate (CI / clean checkout) never
    opens a socket to live gear.
    """
    from sanctum_cli.devices.ha_green import HaGreenProvider

    p = HaGreenProvider()
    try:
        p.connect(Creds(host="10.0.0.3", username="owner", secret=None, key_path=None))
        info = p.get("/api/")
    finally:
        p.disconnect()
    assert info and "API running." in info, "live HA Green returned an unexpected /api/ body"
