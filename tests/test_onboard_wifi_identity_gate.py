"""sanctum onboard — Wi-Fi identity gate: registration, dispatch, skippability,
honest-verify, privacy (SERVER auto-enroll vs ROAMER/UNKNOWN opt-in-only).

The Wi-Fi identity gate is the onboard-time sibling of ``sanctum link optimize``:
on the home SSID it classifies the node (``link.classify_node``) and, ONLY for a
fixed-infra SERVER whose live identity reads QUARANTINED / ROTATING, generates a
MAC-stability profile and narrates the one-click approve. A ROAMER / UNKNOWN node
(a laptop, a phone) is NEVER auto-enrolled — it gets a one-line informational nudge
and the gate configures nothing (privacy-first / per-SSID / home-only).

These tests lock the contracts a reviewer most wants pinned, mirroring the
``ha-green`` gate test that is its sibling in the same "Your Network" chapter:

1. The gate is registered (``RECIPE_GATES``/``_CHAPTER_GATES``/``_GATE_LABELS``),
   dispatched (``_run_gate``), and skippable (``--yes`` short-circuits).
2. HONEST-VERIFY: the green check is derived from a REAL re-probe, never from
   "the step ran"; an UNVERIFIED probe configures nothing (fail-closed).
3. PRIVACY: only a SERVER + at-risk identity auto-enrolls; a ROAMER / UNKNOWN
   node is nudged, never enrolled.

Every network read is a module-level seam (``link.probe_identity`` /
``link.classify_node`` / the profile writer) the tests patch, so no live Wi-Fi /
router / profile install is ever touched.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import patch

import pytest

from sanctum_cli import recipes
from sanctum_cli.commands import onboard
from sanctum_cli.net import link


@pytest.fixture(autouse=True)
def _no_live_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every identity seam to a fail-closed / conservative value so an
    un-stubbed test can NEVER touch the live Wi-Fi / router / a real profile install.

    ``probe_identity`` → an UNVERIFIED probe (fail-closed), ``_node_signals`` → a
    conservative ROAMER-leaning signal, and ``_write_identity_profile`` → a no-op that
    never writes to disk. Tests that exercise a path re-stub the relevant seam.
    """
    monkeypatch.setattr(link, "probe_identity", lambda *a, **k: _probe(verdict_shape="UNVERIFIED"))
    monkeypatch.setattr(
        onboard,
        "_node_signals",
        lambda probe: link.NodeSignals(
            uptime_days=0.0,
            ip_config_method="DHCP",
            ip_is_reserved_or_static=False,
            distinct_ssids_seen=1,
            is_portable=True,
        ),
    )
    monkeypatch.setattr(onboard, "_write_identity_profile", lambda *a, **k: None)


# ── helpers: build deterministic probes / signals without touching hardware ──


def _probe(
    *,
    verdict_shape: str,
) -> link.IdentityProbe:
    """A live-identity probe whose ``diagnose_identity`` verdict is ``verdict_shape``.

    verdict_shape ∈ {"QUARANTINED", "ROTATING", "STABLE", "UNVERIFIED"}.
    """
    if verdict_shape == "UNVERIFIED":
        return link.IdentityProbe(
            iface="",
            ssid=None,
            current_mac="",
            hardware_mac="",
            security=None,
            associated=False,
            router_arp_verified=None,
            gateway_reachable=None,
        )
    # Rotating MAC (locally-administered) ≠ hardware; LAN dead ⇒ QUARANTINED.
    rotating_mac = "7a:11:22:33:44:55"  # locally-administered bit set
    hardware = "20:f8:3b:02:3a:c8"
    if verdict_shape == "STABLE":
        current = hardware
        lan_dead = False
    else:
        current = rotating_mac
        lan_dead = verdict_shape == "QUARANTINED"
    return link.IdentityProbe(
        iface="en0",
        ssid="haus-5G",
        current_mac=current,
        hardware_mac=hardware,
        security="WPA3 Personal",
        associated=True,
        router_arp_verified=(not lan_dead),
        gateway_reachable=(not lan_dead),
    )


# ── 1. Gate registered + dispatched + skippable (the plan's Step 1 test) ─────


def test_wifi_identity_gate_registered_and_runs() -> None:
    """The plan's Step-1 contract: registered in the chapter + every recipe, dispatched."""
    assert "wifi-identity" in onboard._CHAPTER_GATES["Your Network"]
    for r in ("family", "operator", "code"):
        assert "wifi-identity" in onboard.RECIPE_GATES[r]
    with patch.object(onboard, "_run_wifi_identity", return_value=True) as g:
        assert onboard._run_gate("wifi-identity", yes=True) is True
        g.assert_called_once()


def test_gate_registered_data_references_real_recipes() -> None:
    """Every recipe named in RECIPE_GATES is a real built-in; label present."""
    assert set(onboard.RECIPE_GATES) <= set(recipes.BUILTINS)
    assert "wifi-identity" in onboard._GATE_LABELS


def test_gate_wired_into_dispatch_loop() -> None:
    """The 'wifi-identity' branch is actually dispatched — registration alone is not enough."""
    src = inspect.getsource(onboard._run_gate)
    assert 'gate == "wifi-identity"' in src
    assert "_run_wifi_identity(yes=yes)" in src


def test_gate_additive_ordering_before_ha_green_after_network_gear() -> None:
    """Additive: wifi-identity sits AFTER network-gear and BEFORE ha-green (plan §3.1)."""
    gates = onboard.RECIPE_GATES["family"]
    assert gates.index("wifi-identity") > gates.index("network-gear")
    assert gates.index("wifi-identity") < gates.index("ha-green")


def test_gate_skipped_under_yes_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--yes`` SKIPS the gate before any probe/classify/write."""
    calls = {"probe": 0}
    monkeypatch.setattr(
        link, "probe_identity", lambda *a, **k: calls.__setitem__("probe", calls["probe"] + 1)
    )
    assert onboard._run_wifi_identity(yes=True) is False
    assert calls == {"probe": 0}


# ── 2. HONEST-VERIFY / fail-closed ───────────────────────────────────────────


def test_unverified_probe_configures_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe we cannot read (UNVERIFIED) → fail-closed, nothing written, returns False."""
    monkeypatch.setattr(link, "probe_identity", lambda *a, **k: _probe(verdict_shape="UNVERIFIED"))
    monkeypatch.setattr(
        onboard,
        "_node_signals",
        lambda probe: link.NodeSignals(
            uptime_days=99.0,
            ip_config_method="Manual",
            ip_is_reserved_or_static=True,
            distinct_ssids_seen=1,
            is_portable=False,
        ),
    )
    written: list[Any] = []
    monkeypatch.setattr(onboard, "_write_identity_profile", lambda *a, **k: written.append(a))
    assert onboard._run_wifi_identity(yes=False) is False
    assert written == []


def test_stable_server_no_action_no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SERVER already on its hardware MAC (STABLE) → no profile; honest 'already stable'."""
    monkeypatch.setattr(link, "probe_identity", lambda *a, **k: _probe(verdict_shape="STABLE"))
    monkeypatch.setattr(
        onboard,
        "_node_signals",
        lambda probe: link.NodeSignals(
            uptime_days=99.0,
            ip_config_method="Manual",
            ip_is_reserved_or_static=True,
            distinct_ssids_seen=1,
            is_portable=False,
        ),
    )
    written: list[Any] = []
    monkeypatch.setattr(onboard, "_write_identity_profile", lambda *a, **k: written.append(a))
    # STABLE is a real, verified good state → configured True, but NO profile written.
    assert onboard._run_wifi_identity(yes=False) is True
    assert written == []


# ── 3. PRIVACY: SERVER auto-enroll vs ROAMER/UNKNOWN opt-in-only ─────────────


def test_server_quarantined_generates_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """SERVER + QUARANTINED → generate the stability profile + narrate one-click approve."""
    monkeypatch.setattr(link, "probe_identity", lambda *a, **k: _probe(verdict_shape="QUARANTINED"))
    monkeypatch.setattr(
        onboard,
        "_node_signals",
        lambda probe: link.NodeSignals(
            uptime_days=99.0,
            ip_config_method="Manual",
            ip_is_reserved_or_static=True,
            distinct_ssids_seen=1,
            is_portable=False,
        ),
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        onboard,
        "_write_identity_profile",
        lambda probe, out: captured.update(ssid=probe.ssid, mac=probe.hardware_mac),
    )
    assert onboard._run_wifi_identity(yes=False) is True
    assert captured["ssid"] == "haus-5G"
    assert captured["mac"] == "20:f8:3b:02:3a:c8"


def test_server_rotating_generates_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """SERVER + ROTATING (at-risk private MAC, reachable now) → also generate the profile."""
    monkeypatch.setattr(link, "probe_identity", lambda *a, **k: _probe(verdict_shape="ROTATING"))
    monkeypatch.setattr(
        onboard,
        "_node_signals",
        lambda probe: link.NodeSignals(
            uptime_days=99.0,
            ip_config_method="Manual",
            ip_is_reserved_or_static=True,
            distinct_ssids_seen=1,
            is_portable=False,
        ),
    )
    written: list[Any] = []
    monkeypatch.setattr(onboard, "_write_identity_profile", lambda *a, **k: written.append(a))
    assert onboard._run_wifi_identity(yes=False) is True
    assert len(written) == 1


def test_roamer_never_auto_enrolled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ROAMER (portable laptop) even when QUARANTINED → nudge only, NEVER a profile."""
    monkeypatch.setattr(link, "probe_identity", lambda *a, **k: _probe(verdict_shape="QUARANTINED"))
    monkeypatch.setattr(
        onboard,
        "_node_signals",
        lambda probe: link.NodeSignals(
            uptime_days=0.1,
            ip_config_method="DHCP",
            ip_is_reserved_or_static=False,
            distinct_ssids_seen=12,
            is_portable=True,
        ),
    )
    written: list[Any] = []
    monkeypatch.setattr(onboard, "_write_identity_profile", lambda *a, **k: written.append(a))
    assert onboard._run_wifi_identity(yes=False) is False
    assert written == []


def test_unknown_never_auto_enrolled(monkeypatch: pytest.MonkeyPatch) -> None:
    """An UNKNOWN node (insufficient signal → treated as roamer) → nudge only, no profile."""
    monkeypatch.setattr(link, "probe_identity", lambda *a, **k: _probe(verdict_shape="QUARANTINED"))
    monkeypatch.setattr(
        onboard,
        "_node_signals",
        lambda probe: link.NodeSignals(
            uptime_days=0.1,
            ip_config_method="DHCP",
            ip_is_reserved_or_static=False,
            distinct_ssids_seen=1,
            is_portable=False,
        ),
    )
    written: list[Any] = []
    monkeypatch.setattr(onboard, "_write_identity_profile", lambda *a, **k: written.append(a))
    assert onboard._run_wifi_identity(yes=False) is False
    assert written == []
