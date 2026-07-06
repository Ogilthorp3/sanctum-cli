"""haus-scan onboard gate — discover LAN gear + pair each at its own ip.

Every discovery / pairing boundary is a module-level seam the tests replace, so no
live scan / socket / subprocess / Keychain write runs here. The gate is honest-verify
(a device is only "paired" after a real auth-probe against its DISCOVERED ip) and
fail-open (a discovery failure configures nothing but never crashes onboarding).
"""

from __future__ import annotations

import pytest

from sanctum_cli.commands import onboard
from sanctum_cli.gear.types import DiscoveredDevice, HausInventory


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr(onboard, "store_device_secret", lambda **k: None)
    monkeypatch.setattr(onboard, "_revoke_device_secret", lambda **k: None)
    recorded = {}

    def fake_set_ref(*, kind, brand, host, keychain_service, keychain_account, path=None):
        recorded[kind] = {"brand": brand, "host": host}

    monkeypatch.setattr(onboard, "set_device_reference", fake_set_ref)
    monkeypatch.setattr(
        onboard, "_net_context", lambda: onboard._NetContext(gateway_ip="192.168.2.1", runner=None)
    )
    return recorded


def test_haus_scan_yes_skips(monkeypatch):
    called = {"scan": False}
    monkeypatch.setattr(
        onboard,
        "_discover_haus_for_onboard",
        lambda net, allow_active: called.__setitem__("scan", True) or HausInventory([], 0),
    )
    assert onboard._run_haus_scan(yes=True) is False
    assert called["scan"] is False  # --yes: no active scan, no prompts


def test_haus_scan_pairs_discovered_device_with_its_ip(monkeypatch, _no_side_effects):
    dev = DiscoveredDevice(kind="orbi", brand="orbi", ip="10.0.0.5", name="Orbi", score=1.0)
    monkeypatch.setattr(
        onboard, "_discover_haus_for_onboard", lambda net, allow_active: HausInventory([dev], 2)
    )
    monkeypatch.setattr(onboard, "_consent_active_scan", lambda yes: True)
    monkeypatch.setattr(onboard.net_cmd, "device_keychain_ref", lambda kind: ("svc", "acct"))
    monkeypatch.setattr(onboard.Confirm, "ask", lambda *a, **k: True)
    monkeypatch.setattr(onboard.Prompt, "ask", lambda *a, **k: "hunter2")
    monkeypatch.setattr(onboard, "_probe_device", lambda provider, **k: True)
    monkeypatch.setattr(onboard, "_provider_for", lambda kind, ip: object())  # provider stand-in
    assert onboard._run_haus_scan(yes=False) is True
    assert _no_side_effects["orbi"] == {"brand": "orbi", "host": "10.0.0.5"}  # PAIRED WITH DISCOVERED IP


def test_haus_scan_failopen_when_discovery_raises(monkeypatch):
    def boom(net, allow_active):
        raise OSError("scan blew up")

    monkeypatch.setattr(onboard, "_discover_haus_for_onboard", boom)
    monkeypatch.setattr(onboard, "_consent_active_scan", lambda yes: True)
    # Must NOT raise; a failed scan configures nothing but never crashes onboarding.
    assert onboard._run_haus_scan(yes=False) is False
