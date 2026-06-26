"""Multi-transport router scaffold — API now, agent-browser → android as fallbacks.

The router answers, per :class:`~sanctum_cli.devices.base.Capability`, which
transport drives a setting:

* the **API** transport (the existing DeviceProvider ops) when the provider
  advertises a REAL op for the capability (it is in ``capabilities()`` /
  ``capability_map().bindings``); else
* the **GUI fallback** transport (agent-browser, then android) for a surface the
  API cannot reach (``capability_map().ceiling``) — a Phase-2 live recipe that is
  STUBBED (``execute`` raises ``NotImplementedError('Phase 2: live recipe')``) but
  is cred-resolved through the headless resolver so it is authenticated-ready.

These tests are derived from the REAL provider contracts (the installed
``sagemcom_api`` / ``pynetgear`` seams, mocked at the factory; the Firewalla
bridge httpx, mocked at ``_fetch_bridge_json``) — never a fake that shares the
producer's assumptions (Contracts at the Boundary). The honesty axes:

* the 3 honesty-defect caps — Orbi ``AP_MODE`` + ``CHANNELS`` and Firewalla
  ``WAN_MODE`` — have NO live API op, so the router must route them to the GUI
  fallback (never advertise a live transport they lack); and
* the GUI-only ceiling is named, and every ceiling route is a Phase-2 stub.
"""

from __future__ import annotations

import pytest

from sanctum_cli.devices import creds as creds_resolver
from sanctum_cli.devices import firewalla as fw_mod
from sanctum_cli.devices import transport as transport_mod
from sanctum_cli.devices.base import Capability, Creds
from sanctum_cli.devices.firewalla import FirewallaProvider
from sanctum_cli.devices.orbi import OrbiProvider
from sanctum_cli.devices.registry import GenericReadOnlyProvider
from sanctum_cli.devices.sagemcom import SagemcomHubProvider
from sanctum_cli.devices.transport import (
    PHASE2_RECIPE_MSG,
    PRIORITY,
    ApiTransport,
    CapabilityRoute,
    GuiRecipeTransport,
    RoutePlan,
    TransportKind,
    build_transport,
    plan_routes,
    select_transport,
)

BRIDGE_PATH = "Device/Services/BellNetworkCfg/SetBridgeMode"
DMZ_PATH = "Device/Services/BellNetworkCfg/AdvancedDMZ"


# ── a minimal real-async Sagemcom client (the vendor seam, mocked) ───────────


class _FakeSah:
    """Stand-in for ``sagemcom_api.client.SagemcomClient`` — records set calls.

    Exposes the exact async surface the provider drives on its persistent loop
    (login/logout/close + get/set by xpath). ``set_value_by_xpath`` returns the
    real ``XMO_NO_ERR`` reply envelope the provider's fail-closed inspector reads,
    so an :class:`ApiTransport` op is exercised through the genuine reply-handling.
    """

    def __init__(self, values: dict[str, str | None]) -> None:
        self._v: dict[str, str | None] = dict(values)
        self.set_calls: list[tuple[str, str]] = []

    async def login(self) -> None:
        return None

    async def logout(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get_value_by_xpath(self, xpath: str, options: dict | None = None) -> str | None:
        return self._v.get(xpath)

    async def set_value_by_xpath(
        self, xpath: str, value: str, options: dict | None = None
    ) -> dict:
        self.set_calls.append((xpath, value))
        self._v[xpath] = value
        return {"reply": {"error": {"description": "XMO_NO_ERR"}}}


_OPENED: list = []


@pytest.fixture(autouse=True)
def _disconnect_opened():
    _OPENED.clear()
    yield
    while _OPENED:
        _OPENED.pop().disconnect()


def _connected_sagemcom(monkeypatch: pytest.MonkeyPatch, fake: _FakeSah) -> SagemcomHubProvider:
    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    p = SagemcomHubProvider()
    p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
    _OPENED.append(p)
    return p


# ── TransportKind + the priority chain ───────────────────────────────────────


def test_transport_kind_values_and_priority_chain() -> None:
    """API → agent-browser → android, in that fixed priority order."""
    assert TransportKind.API == "api"
    assert TransportKind.BROWSER == "agent-browser"
    assert TransportKind.ANDROID == "android"
    assert list(PRIORITY) == [TransportKind.API, TransportKind.BROWSER, TransportKind.ANDROID]


# ── per-brand fallback transport (the real fact) ─────────────────────────────


def test_fallback_transport_is_browser_for_web_ui_brands() -> None:
    """Sagemcom (Bell hub web UI) + Orbi (web UI + app) fall back to agent-browser."""
    assert SagemcomHubProvider().fallback_transport() is TransportKind.BROWSER
    assert OrbiProvider().fallback_transport() is TransportKind.BROWSER


def test_fallback_transport_is_android_for_app_only_firewalla() -> None:
    """Firewalla has NO admin web UI — NAT/DMZ/WAN/VPN are reachable only via the
    Firewalla mobile app, so its GUI fallback is android."""
    assert FirewallaProvider().fallback_transport() is TransportKind.ANDROID


def test_generic_provider_defaults_to_browser_fallback() -> None:
    """A provider that does not declare a fallback defaults to the chain's first
    non-API rung (agent-browser)."""
    assert transport_mod.fallback_kind(GenericReadOnlyProvider("hub")) is TransportKind.BROWSER


# ── select_transport: API for a real cap, fallback for a ceiling cap ──────────


def test_select_transport_api_for_real_sagemcom_cap() -> None:
    p = SagemcomHubProvider()
    assert Capability.BRIDGE_MODE in p.capabilities()
    assert select_transport(p, Capability.BRIDGE_MODE) is TransportKind.API


def test_select_transport_orbi_ap_mode_and_channels_route_to_fallback() -> None:
    """The Orbi honesty defects: pynetgear has NO set-AP-mode / set-channel SOAP
    action, so AP_MODE + CHANNELS have no live API op → routed to agent-browser."""
    p = OrbiProvider()
    assert Capability.AP_MODE not in p.capabilities()
    assert Capability.CHANNELS not in p.capabilities()
    assert select_transport(p, Capability.AP_MODE) is TransportKind.BROWSER
    assert select_transport(p, Capability.CHANNELS) is TransportKind.BROWSER
    # A cap with a REAL pynetgear write still routes to API.
    assert select_transport(p, Capability.GUEST_WIFI) is TransportKind.API


def test_select_transport_firewalla_wan_mode_routes_to_android(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Firewalla honesty defect: WAN_MODE/NAT/DMZ are GUI-only (no bridge
    route), so WAN_MODE routes to the app (android); a real bridge cap → API."""
    monkeypatch.setattr(fw_mod, "_fetch_bridge_json", lambda *a, **k: None)
    p = FirewallaProvider()
    assert Capability.WAN_MODE not in p.capabilities()
    assert select_transport(p, Capability.WAN_MODE) is TransportKind.ANDROID
    assert select_transport(p, Capability.READ) is TransportKind.API


# ── plan_routes: API routes ≡ real bindings; ceiling ≡ Phase-2 fallback ───────


def test_plan_routes_sagemcom_api_routes_match_real_bindings() -> None:
    p = SagemcomHubProvider()
    plan = plan_routes(p)
    assert isinstance(plan, RoutePlan)
    cmap = p.capability_map()
    api = [r for r in plan.routes if r.transport is TransportKind.API]
    # No phantom, no gap: API routes cover EXACTLY the real capability bindings.
    assert {r.capability for r in api} == {b.capability for b in cmap.bindings}
    assert all(r.live for r in api)
    # Each API route carries the concrete real op text (cross-checked vs binding).
    by_cap = {b.capability: b for b in cmap.bindings}
    for r in api:
        assert r.capability is not None
        assert by_cap[r.capability].op in r.op


def test_plan_routes_sagemcom_ceiling_is_phase2_browser_stub() -> None:
    p = SagemcomHubProvider()
    plan = plan_routes(p)
    cmap = p.capability_map()
    gui = [r for r in plan.routes if r.transport is not TransportKind.API]
    # The ceiling routes cover EXACTLY the named GUI-only surfaces.
    assert {r.setting for r in gui} == set(cmap.ceiling)
    assert gui  # the Bell hub has a carrier-locked ceiling
    for r in gui:
        assert isinstance(r, CapabilityRoute)
        assert r.transport is TransportKind.BROWSER
        assert r.live is False
        assert r.op == PHASE2_RECIPE_MSG
        assert r.capability is None  # a ceiling surface is not an advertised cap
    assert plan.fallback is TransportKind.BROWSER


def test_plan_routes_orbi_defects_appear_only_in_ceiling() -> None:
    """AP_MODE/CHANNELS must never be live API routes; the ceiling names them."""
    p = OrbiProvider()
    plan = plan_routes(p)
    live_caps = {r.capability for r in plan.routes if r.live}
    assert Capability.AP_MODE not in live_caps
    assert Capability.CHANNELS not in live_caps
    ceiling_blob = " ".join(r.setting for r in plan.routes if not r.live).lower()
    for surface in ("ssid", "channel", "ap", "port", "ipv6", "vpn"):
        assert surface in ceiling_blob
    assert all(
        r.transport is TransportKind.BROWSER for r in plan.routes if not r.live
    )


def test_plan_routes_firewalla_ceiling_is_android(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fw_mod, "_fetch_bridge_json", lambda *a, **k: None)
    p = FirewallaProvider()
    plan = plan_routes(p)
    gui = [r for r in plan.routes if not r.live]
    assert gui
    assert all(r.transport is TransportKind.ANDROID for r in gui)
    assert all(r.op == PHASE2_RECIPE_MSG for r in gui)
    blob = " ".join(r.setting for r in gui).lower()
    for surface in ("nat", "dmz", "wan", "vpn"):
        assert surface in blob
    # WAN_MODE never appears as a live API route.
    assert all(r.capability is not Capability.WAN_MODE for r in plan.routes if r.live)


def test_plan_routes_generic_provider_has_no_ceiling() -> None:
    """A degraded provider (no capability_map) lists its API caps with no ceiling."""
    p = GenericReadOnlyProvider("hub")
    plan = plan_routes(p)
    assert {r.capability for r in plan.routes} == {Capability.READ}
    assert all(r.live for r in plan.routes)
    assert plan.fallback is TransportKind.BROWSER


# ── GUI fallback transport: Phase-2 stub + cred wiring (authenticated-ready) ──


def test_browser_transport_execute_is_phase2_stub() -> None:
    t = GuiRecipeTransport(
        TransportKind.BROWSER, account="admin", service="bell-hub-admin", resolver=lambda a, s: "pw"
    )
    assert t.kind is TransportKind.BROWSER
    assert t.live is False
    with pytest.raises(NotImplementedError, match="Phase 2: live recipe"):
        t.execute(Capability.AP_MODE)


def test_android_transport_execute_is_phase2_stub() -> None:
    t = GuiRecipeTransport(
        TransportKind.ANDROID, account="", service="firewalla-app", resolver=lambda a, s: "tok"
    )
    assert t.kind is TransportKind.ANDROID
    with pytest.raises(NotImplementedError, match="Phase 2: live recipe"):
        t.execute(Capability.WAN_MODE)


def test_gui_transport_resolves_creds_via_injected_resolver() -> None:
    """Authenticated-ready: the transport resolves its secret via the resolver and
    reports authenticated() iff a secret came back — under the (account, service)
    it was wired with."""
    calls: list[tuple[str, str]] = []

    def fake_resolver(account: str, service: str) -> str | None:
        calls.append((account, service))
        return "secret"

    t = GuiRecipeTransport(
        TransportKind.ANDROID, account="", service="firewalla-app", resolver=fake_resolver
    )
    assert t.authenticated() is True
    assert calls == [("", "firewalla-app")]


def test_gui_transport_unauthenticated_when_no_cred() -> None:
    t = GuiRecipeTransport(
        TransportKind.BROWSER, account="admin", service="orbi-admin", resolver=lambda a, s: None
    )
    assert t.authenticated() is False


def test_gui_transport_default_resolver_is_headless_resolve_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DEFAULT cred resolver is the headless ``resolve_secret_optional``
    (Keychain → SOPS → NEVER op/1Password) — proven by routing through it."""
    captured: dict[str, tuple[str, str]] = {}

    def fake_optional(account: str, service: str) -> str | None:
        captured["args"] = (account, service)
        return "pw"

    monkeypatch.setattr(creds_resolver, "resolve_secret_optional", fake_optional)
    t = GuiRecipeTransport(
        TransportKind.BROWSER, account="admin", service="bell-hub-admin"
    )
    assert t.authenticated() is True
    assert captured["args"] == ("admin", "bell-hub-admin")


# ── ApiTransport: the live "API (now)" executor, through to the vendor seam ───


def test_api_transport_execute_drives_real_setvalue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cross-layer contract: ApiTransport → provider.capability_op → provider.set
    → the REAL sagemcom_api set_value_by_xpath seam (recorded by the fake)."""
    fake = _FakeSah({BRIDGE_PATH: "off"})
    p = _connected_sagemcom(monkeypatch, fake)
    t = build_transport(p, TransportKind.API)
    assert isinstance(t, ApiTransport)
    res = t.execute(Capability.BRIDGE_MODE, "on")
    assert res.ok
    assert (BRIDGE_PATH, "on") in fake.set_calls


def test_api_transport_execute_no_value_uses_capability_engaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no explicit value the transport writes the capability_op's engaged value."""
    fake = _FakeSah({DMZ_PATH: "off"})
    p = _connected_sagemcom(monkeypatch, fake)
    res = ApiTransport(p).execute(Capability.DMZ)  # engaged == "on"
    assert res.ok
    assert (DMZ_PATH, "on") in fake.set_calls


def test_api_transport_execute_unsupported_cap_is_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capability the provider has no op for yields ok=False (never a phantom write)."""
    fake = _FakeSah({BRIDGE_PATH: "off"})
    p = _connected_sagemcom(monkeypatch, fake)
    res = ApiTransport(p).execute(Capability.POLICY)  # sagemcom has no POLICY op
    assert res.ok is False
    assert fake.set_calls == []


def test_api_transport_authenticated_reflects_provider_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ApiTransport.authenticated() delegates to the provider's auth oracle."""
    fake = _FakeSah({BRIDGE_PATH: "off"})
    p = _connected_sagemcom(monkeypatch, fake)
    assert ApiTransport(p).authenticated() is True


def test_build_transport_factory_returns_gui_stub_for_non_api() -> None:
    t = build_transport(
        GenericReadOnlyProvider("orbi"),
        TransportKind.BROWSER,
        account="admin",
        service="orbi-admin",
        resolver=lambda a, s: "pw",
    )
    assert isinstance(t, GuiRecipeTransport)
    assert t.kind is TransportKind.BROWSER
    with pytest.raises(NotImplementedError, match="Phase 2: live recipe"):
        t.execute(Capability.AP_MODE)
