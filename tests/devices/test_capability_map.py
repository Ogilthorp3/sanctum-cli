"""``capability_map()`` / ``list_paths()`` — the honest "what can I change on THIS
box" answer for each brand.

Two honesty invariants, asserted per provider:

* **No phantom, no gap.** The capabilities a map BINDS are exactly the ones the
  provider advertises in :meth:`capabilities` — every advertised cap names a REAL
  transport + op (never an advertised cap with no backing op), and no binding
  names a cap the provider does not advertise. This is the honest-verify doctrine
  expressed as a set equality, derived from the provider's OWN ``capabilities()``
  (a different source than the binding table — Contracts at the Boundary §2).
* **The ceiling is named, not implied.** Each map carries the GUI-only / carrier-
  locked surfaces the transport CANNOT reach (Orbi SSID/channel/port-forward/
  IPv6/VPN; Firewalla NAT/DMZ/WAN/VPN; Sagemcom carrier-locked leaves), and those
  surfaces are NEVER also bound — so the map can't claim a power the box lacks.
"""

from __future__ import annotations

import pytest

from sanctum_cli.devices import firewalla as fw_mod
from sanctum_cli.devices.base import Capability, CapabilityBinding, CapabilityMap
from sanctum_cli.devices.firewalla import FirewallaProvider
from sanctum_cli.devices.orbi import OrbiProvider
from sanctum_cli.devices.sagemcom import SagemcomHubProvider


def _caps_in_map(cmap: CapabilityMap) -> set[Capability]:
    return {b.capability for b in cmap.bindings}


def _ceiling_blob(cmap: CapabilityMap) -> str:
    return " ".join(cmap.ceiling).lower()


# ── shape ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider_cls", [SagemcomHubProvider, OrbiProvider])
def test_capability_map_shape_and_bindings_are_real(provider_cls: type) -> None:
    """Each binding carries a non-empty transport AND a non-empty real op."""
    p = provider_cls()
    cmap = p.capability_map()
    assert isinstance(cmap, CapabilityMap)
    assert cmap.brand == p.brand
    assert cmap.bindings, "a real device must bind at least its READ capability"
    for b in cmap.bindings:
        assert isinstance(b, CapabilityBinding)
        assert b.transport, f"{b.capability}: transport must name how the op reaches the box"
        assert b.op, f"{b.capability}: op must name the concrete real op/route/path"
    # list_paths() is the flat rendering of the same bindings.
    assert list(p.list_paths()) == list(cmap.bindings)


@pytest.mark.parametrize("provider_cls", [SagemcomHubProvider, OrbiProvider])
def test_map_binds_exactly_advertised_capabilities(provider_cls: type) -> None:
    """Honest-verify: bound caps == advertised caps (no phantom op, no missing op)."""
    p = provider_cls()
    assert _caps_in_map(p.capability_map()) == set(p.capabilities())


# ── Sagemcom: near-total setValue surface + carrier-locked ceiling ───────────


def test_sagemcom_map_binds_real_setvalue_leaves() -> None:
    p = SagemcomHubProvider()
    cmap = p.capability_map()
    by_cap = {b.capability: b for b in cmap.bindings}
    # BRIDGE_MODE binds the REAL Bell leaf via setValue (the single-NAT control).
    assert "SetBridgeMode" in by_cap[Capability.BRIDGE_MODE].op
    assert "set" in by_cap[Capability.BRIDGE_MODE].transport.lower()
    # DMZ binds the real Advanced-DMZ leaf.
    assert "AdvancedDMZ" in by_cap[Capability.DMZ].op


def test_sagemcom_ceiling_names_carrier_lock() -> None:
    p = SagemcomHubProvider()
    blob = _ceiling_blob(p.capability_map())
    # The two writability walls the audit found: firmware NON_WRITABLE + Bell
    # ACCESS_RESTRICTION (carrier-locked). The ceiling must name the lock.
    assert "carrier" in blob or "access_restriction" in blob or "non_writable" in blob


# ── Orbi: fixed SOAP writes; AP_MODE/CHANNELS are GUI-only (the honesty defect) ──


def test_orbi_map_binds_real_pynetgear_methods() -> None:
    p = OrbiProvider()
    by_cap = {b.capability: b for b in p.capability_map().bindings}
    assert "reboot" in by_cap[Capability.REBOOT].op
    assert "set_speed_test_start" in by_cap[Capability.SPEEDTEST].op
    # FEATURE_TOGGLE is backed by a REAL pynetgear setter (qos/smart-connect/meter).
    assert "set_qos_enable_status" in by_cap[Capability.FEATURE_TOGGLE].op
    assert "soap" in by_cap[Capability.REBOOT].transport.lower()


def test_orbi_does_not_bind_ap_mode_or_channels() -> None:
    """The 3 honesty defects: pynetgear has NO SOAP write for AP-mode/channel — so
    capability_map must NOT bind them, and the ceiling must name them GUI-only."""
    p = OrbiProvider()
    cmap = p.capability_map()
    bound = _caps_in_map(cmap)
    assert Capability.AP_MODE not in bound
    assert Capability.CHANNELS not in bound
    blob = _ceiling_blob(cmap)
    for surface in ("ssid", "channel", "port", "ipv6", "vpn"):
        assert surface in blob, f"Orbi ceiling must name the GUI-only {surface!r} surface"


# ── Firewalla: bridge routes; NAT/DMZ/WAN/VPN are GUI-only ───────────────────


def test_firewalla_map_binds_real_bridge_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    # /info unreachable → enforcement state UNKNOWN → enforcement caps kept (routes exist).
    monkeypatch.setattr(fw_mod, "_fetch_bridge_json", lambda *a, **k: None)
    p = FirewallaProvider()
    cmap = p.capability_map()
    assert _caps_in_map(cmap) == set(p.capabilities())
    by_cap = {b.capability: b for b in cmap.bindings}
    assert "/host/" in by_cap[Capability.DEVICE_POLICY].op and "policy" in by_cap[
        Capability.DEVICE_POLICY
    ].op
    assert "/dns" in by_cap[Capability.LOCAL_DNS].op
    assert "/speedtest" in by_cap[Capability.SPEEDTEST].op
    assert "bridge" in by_cap[Capability.READ].transport.lower()


def test_firewalla_does_not_bind_wan_mode_and_names_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Honesty defect: WAN_MODE/NAT/DMZ/VPN are GUI-only on a Firewalla — never bound."""
    monkeypatch.setattr(fw_mod, "_fetch_bridge_json", lambda *a, **k: None)
    p = FirewallaProvider()
    cmap = p.capability_map()
    assert Capability.WAN_MODE not in _caps_in_map(cmap)
    blob = _ceiling_blob(cmap)
    for surface in ("nat", "dmz", "wan", "vpn"):
        assert surface in blob, f"Firewalla ceiling must name the GUI-only {surface!r} surface"


def test_firewalla_map_drops_enforcement_caps_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit enforcement_ready=false shrinks BOTH capabilities() and the map."""
    monkeypatch.setattr(
        fw_mod,
        "_fetch_bridge_json",
        lambda *a, **k: {"capabilities": {"enforcement_ready": False}},
    )
    p = FirewallaProvider()
    cmap = p.capability_map()
    bound = _caps_in_map(cmap)
    assert Capability.DEVICE_POLICY not in bound  # enforcement cap dropped
    assert Capability.READ in bound  # base cap stays
    assert bound == set(p.capabilities())  # still no drift
