"""Tests for the pure `sanctum net status` roll-up assembler.

The assembler (:func:`build_status_report`) is a PURE function over already-probed
subsystem inputs → per-row statuses + an OVERALL verdict. No live calls; the CLI
handler (tested in tests/test_net_cli.py) does the impure probing and feeds these
value objects in. These tests pin the truth table: all-green → GREEN; a
continuous-protection layer down (heal daemon not loaded, guardian stale,
identity quarantined, spine down) → DEGRADED; and a subsystem whose probe failed
(UNKNOWN) still renders and never crashes.
"""

from __future__ import annotations

from sanctum_cli.net.status import (
    DaemonInfo,
    GuardianInfo,
    RowStatus,
    SpineInfo,
    StatusReport,
    build_status_report,
)

# ─── input fixtures (the six subsystem value objects) ─────────────────


def _healthy_posture_diag():
    """A HEALTHY posture diagnosis (DHCP, reachable gateway, on-subnet)."""
    from sanctum_cli.net.heal import HealAction, NetPosture, PostureDiagnosis

    posture = NetPosture(
        iface="en1",
        config_method="DHCP",
        ip="192.168.2.20",
        subnet="255.255.255.0",
        gateway="192.168.2.1",
        gateway_reachable=True,
        associated=True,
        on_tailnet=True,
        tb5_up=True,
    )
    return PostureDiagnosis(
        verdict="HEALTHY",
        detail="Posture is healthy — DHCP, reachable gateway, on-subnet.",
        remedy="",
        action=HealAction("none", safe=True, detail="no action"),
        posture=posture,
    )


def _drift_posture_diag():
    """A STATIC_DRIFT posture diagnosis (Manual/static address)."""
    from sanctum_cli.net.heal import HealAction, NetPosture, PostureDiagnosis

    posture = NetPosture(
        iface="en1",
        config_method="Manual",
        ip="10.0.0.10",
        subnet="255.255.255.0",
        gateway="10.0.0.1",
        gateway_reachable=False,
        associated=True,
        on_tailnet=True,
        tb5_up=True,
    )
    return PostureDiagnosis(
        verdict="STATIC_DRIFT",
        detail="Interface is on a Manual (static) address.",
        remedy='Flip Wi-Fi to DHCP (networksetup -setdhcp "Wi-Fi").',
        action=HealAction("flip_dhcp", safe=True, detail="Manual → DHCP"),
        posture=posture,
    )


def _stable_identity():
    from sanctum_cli.net.link import IdentityDiagnosis, IdentityProbe

    probe = IdentityProbe(
        iface="en1",
        ssid="Manoir",
        current_mac="d0:11:e5:1c:88:59",
        hardware_mac="d0:11:e5:1c:88:59",
        security="WPA3",
        associated=True,
        router_arp_verified=True,
        gateway_reachable=True,
    )
    return IdentityDiagnosis(
        "IDENTITY_STABLE", "on hardware MAC", "identity is correct", probe
    )


def _quarantined_identity():
    from sanctum_cli.net.link import IdentityDiagnosis, IdentityProbe

    probe = IdentityProbe(
        iface="en1",
        ssid="Manoir",
        current_mac="32:a6:f4:de:54:cf",
        hardware_mac="d0:11:e5:1c:88:59",
        security="WPA3",
        associated=True,
        router_arp_verified=False,
        gateway_reachable=False,
    )
    return IdentityDiagnosis(
        "IDENTITY_QUARANTINED", "rotating MAC, gateway dead", "pin the MAC", probe
    )


def _topology_single():
    from sanctum_cli.net.types import Nat, TopologyReport

    return TopologyReport(
        firewalla_present=True,
        firewalla_wan_mac="20:6d:31:51:67:82",
        firewalla_wan_mtu=1500,
        nat=Nat.SINGLE,
        gateway_ip="192.168.2.1",
        isp="bell",
        public_ip="1.2.3.4",
        applicable=False,
        reason="Already single-NAT — your network is optimal.",
    )


def _all_green_inputs():
    return {
        "posture": _healthy_posture_diag(),
        "spine": SpineInfo(on_tailnet=True, tb5_up=True),
        "daemon": DaemonInfo(loaded=True, last_result="healed", age_seconds=60, fresh=True),
        "identity": _stable_identity(),
        "topology": _topology_single(),
        "guardian": GuardianInfo(reachable=True, fresh=True, age_seconds=120),
    }


# ─── all-green → GREEN ────────────────────────────────────────────────


def test_all_green_overall_is_green() -> None:
    rep = build_status_report(**_all_green_inputs())
    assert isinstance(rep, StatusReport)
    assert rep.overall == "GREEN"


def test_all_green_every_row_ok() -> None:
    rep = build_status_report(**_all_green_inputs())
    labels = {row.label for row in rep.rows}
    # All six subsystems roll up.
    assert {"Posture", "Spine", "Heal daemon", "Identity", "Topology", "Guardian"} <= labels
    assert all(row.status is RowStatus.OK for row in rep.rows)


def test_report_is_frozen() -> None:
    rep = build_status_report(**_all_green_inputs())
    # StatusReport is an immutable value object (frozen dataclass).
    assert type(rep).__dataclass_params__.frozen is True  # type: ignore[attr-defined]


# ─── posture DEGRADED (STATIC_DRIFT) → ATTENTION, not full outage ─────


def test_static_drift_posture_row_attention() -> None:
    inp = _all_green_inputs()
    inp["posture"] = _drift_posture_diag()
    rep = build_status_report(**inp)
    posture_row = next(r for r in rep.rows if r.label == "Posture")
    assert posture_row.status is RowStatus.ATTENTION
    assert "STATIC_DRIFT" in posture_row.detail
    # A drifted posture is not a continuous-protection outage — overall is ATTENTION.
    assert rep.overall == "ATTENTION"


# ─── heal daemon down → DEGRADED (continuous protection lost) ─────────


def test_heal_daemon_not_loaded_is_degraded() -> None:
    inp = _all_green_inputs()
    inp["daemon"] = DaemonInfo(loaded=False, last_result=None)
    rep = build_status_report(**inp)
    daemon_row = next(r for r in rep.rows if r.label == "Heal daemon")
    assert daemon_row.status is RowStatus.DOWN
    assert rep.overall == "DEGRADED"


# ─── guardian stale → DEGRADED ────────────────────────────────────────


def test_guardian_stale_is_degraded() -> None:
    inp = _all_green_inputs()
    inp["guardian"] = GuardianInfo(reachable=True, fresh=False, age_seconds=3600)
    rep = build_status_report(**inp)
    guardian_row = next(r for r in rep.rows if r.label == "Guardian")
    assert guardian_row.status is RowStatus.DOWN
    assert rep.overall == "DEGRADED"


def test_guardian_unknown_is_not_degraded() -> None:
    # Best-effort: an unreachable/unknown guardian must NOT drag the whole node to
    # DEGRADED (it is optional). It renders UNKNOWN and the overall stays GREEN.
    inp = _all_green_inputs()
    inp["guardian"] = GuardianInfo(reachable=False, fresh=None, age_seconds=None)
    rep = build_status_report(**inp)
    guardian_row = next(r for r in rep.rows if r.label == "Guardian")
    assert guardian_row.status is RowStatus.UNKNOWN
    assert rep.overall == "GREEN"


# ─── identity QUARANTINED → DEGRADED ──────────────────────────────────


def test_identity_quarantined_is_degraded() -> None:
    inp = _all_green_inputs()
    inp["identity"] = _quarantined_identity()
    rep = build_status_report(**inp)
    identity_row = next(r for r in rep.rows if r.label == "Identity")
    assert identity_row.status is RowStatus.DOWN
    assert "IDENTITY_QUARANTINED" in identity_row.detail
    assert rep.overall == "DEGRADED"


def test_identity_rotating_is_attention_not_degraded() -> None:
    # ROTATING works now but is at-risk — ATTENTION, not an outage.
    from sanctum_cli.net.link import IdentityDiagnosis, IdentityProbe

    probe = IdentityProbe(
        iface="en1", ssid="Manoir", current_mac="32:a6:f4:de:54:cf",
        hardware_mac="d0:11:e5:1c:88:59", security="WPA3", associated=True,
        router_arp_verified=True, gateway_reachable=True,
    )
    inp = _all_green_inputs()
    inp["identity"] = IdentityDiagnosis(
        "IDENTITY_ROTATING", "rotating MAC; reachable", "pin it", probe
    )
    rep = build_status_report(**inp)
    identity_row = next(r for r in rep.rows if r.label == "Identity")
    assert identity_row.status is RowStatus.ATTENTION
    assert rep.overall == "ATTENTION"


# ─── spine down → DEGRADED ────────────────────────────────────────────


def test_spine_down_is_degraded() -> None:
    inp = _all_green_inputs()
    inp["spine"] = SpineInfo(on_tailnet=False, tb5_up=False)
    rep = build_status_report(**inp)
    spine_row = next(r for r in rep.rows if r.label == "Spine")
    assert spine_row.status is RowStatus.DOWN
    assert rep.overall == "DEGRADED"


def test_spine_one_leg_up_is_ok() -> None:
    # Either leg alive keeps the never-strand spine OK (out-of-band path survives).
    inp = _all_green_inputs()
    inp["spine"] = SpineInfo(on_tailnet=True, tb5_up=False)
    rep = build_status_report(**inp)
    spine_row = next(r for r in rep.rows if r.label == "Spine")
    assert spine_row.status is RowStatus.OK
    assert "tailnet ✓" in spine_row.detail
    assert "TB5 ✗" in spine_row.detail


# ─── a subsystem UNKNOWN (probe failed) still renders, never crashes ──


def test_none_subsystems_render_unknown_and_do_not_crash() -> None:
    # Every subsystem probe failed (the CLI fed None for each). The assembler must
    # still produce all six rows as UNKNOWN and never raise.
    rep = build_status_report(
        posture=None,
        spine=None,
        daemon=None,
        identity=None,
        topology=None,
        guardian=None,
    )
    assert len(rep.rows) == 6
    assert all(r.status is RowStatus.UNKNOWN for r in rep.rows)


def test_posture_unknown_does_not_force_degraded() -> None:
    # A failed posture probe (UNKNOWN) is not proof of a protection outage — it must
    # not by itself flip the node to DEGRADED (fail-open on unknowns for the roll-up
    # verdict; the continuous-protection DOWN signals are what drive DEGRADED).
    inp = _all_green_inputs()
    inp["posture"] = None
    rep = build_status_report(**inp)
    posture_row = next(r for r in rep.rows if r.label == "Posture")
    assert posture_row.status is RowStatus.UNKNOWN
    assert rep.overall == "GREEN"


def test_posture_unverified_is_unknown() -> None:
    # A posture that probed but read UNVERIFIED (couldn't read the iface) renders as
    # UNKNOWN — we could not verify it, not that it is broken.
    from sanctum_cli.net.heal import HealAction, NetPosture, PostureDiagnosis

    posture = NetPosture(
        iface="", config_method="", ip="", subnet="", gateway="",
        gateway_reachable=None, associated=False, on_tailnet=False, tb5_up=False,
    )
    diag = PostureDiagnosis(
        verdict="UNVERIFIED",
        detail="Could not read the network posture.",
        remedy="Re-run once connected.",
        action=HealAction("alert_only", safe=False, detail="posture unread"),
        posture=posture,
    )
    inp = _all_green_inputs()
    inp["posture"] = diag
    rep = build_status_report(**inp)
    posture_row = next(r for r in rep.rows if r.label == "Posture")
    assert posture_row.status is RowStatus.UNKNOWN


def test_daemon_last_result_surfaced_in_detail() -> None:
    inp = _all_green_inputs()
    inp["daemon"] = DaemonInfo(
        loaded=True, last_result="reverted", age_seconds=30, fresh=True
    )
    rep = build_status_report(**inp)
    daemon_row = next(r for r in rep.rows if r.label == "Heal daemon")
    assert "reverted" in daemon_row.detail
    # Loaded daemon is OK even if the last cycle reverted — it is running/guarding.
    assert daemon_row.status is RowStatus.OK


# ─── MUST-FIX [HIGH]: a loaded-but-WEDGED daemon must NOT read GREEN ──
#
# The heal daemon is a *continuous-protection* layer: it is only doing its job if
# it is heart-beating on its LaunchDaemon interval (HEAL_INTERVAL_S=120s). A daemon
# that launchctl reports "loaded" but whose last heartbeat is hours old is WEDGED —
# it is not guarding anything. The old assembler mapped loaded=True → OK
# unconditionally (it discarded the heartbeat timestamp entirely), so a wedged
# daemon read GREEN. These pin that a stale heartbeat degrades the row to DOWN and
# drags the overall verdict to DEGRADED — mirroring how the guardian row already
# gates on freshness.


def test_daemon_loaded_but_stale_heartbeat_is_down_and_degraded() -> None:
    # Loaded, but the last heartbeat is far older than the freshness window (a
    # wedged daemon that stopped firing). It must NOT read OK/GREEN — the
    # continuous-protection layer is effectively DOWN → overall DEGRADED.
    inp = _all_green_inputs()
    inp["daemon"] = DaemonInfo(
        loaded=True, last_result="healed", age_seconds=7200, fresh=False
    )
    rep = build_status_report(**inp)
    daemon_row = next(r for r in rep.rows if r.label == "Heal daemon")
    assert daemon_row.status is RowStatus.DOWN
    assert "stale" in daemon_row.detail.lower()
    assert "7200s" in daemon_row.detail
    assert rep.overall == "DEGRADED"


def test_daemon_loaded_and_fresh_heartbeat_is_ok() -> None:
    # Loaded AND heart-beating within the freshness window → OK, overall GREEN.
    inp = _all_green_inputs()
    inp["daemon"] = DaemonInfo(
        loaded=True, last_result="healed", age_seconds=60, fresh=True
    )
    rep = build_status_report(**inp)
    daemon_row = next(r for r in rep.rows if r.label == "Heal daemon")
    assert daemon_row.status is RowStatus.OK
    assert rep.overall == "GREEN"


def test_daemon_loaded_unknown_age_is_ok_not_stale() -> None:
    # Backward-compatible fail-open: if we could not compute the heartbeat age
    # (no timestamp / unreadable), a loaded daemon is NOT declared stale — it
    # stays OK (an unread freshness is not proof of a wedge, matching the
    # fail-open-on-unknown doctrine of the roll-up).
    inp = _all_green_inputs()
    inp["daemon"] = DaemonInfo(
        loaded=True, last_result="healed", age_seconds=None, fresh=None
    )
    rep = build_status_report(**inp)
    daemon_row = next(r for r in rep.rows if r.label == "Heal daemon")
    assert daemon_row.status is RowStatus.OK
    assert rep.overall == "GREEN"


def test_daemon_stop_token_is_down_no_loop_cap() -> None:
    # The wrapper writes a "STOP …" heartbeat when the no-loop cap is hit (auto-heal
    # paused). Even fresh, that is a protection outage — surface DOWN, not a benign
    # "last: STOP" on an OK row.
    inp = _all_green_inputs()
    inp["daemon"] = DaemonInfo(
        loaded=True,
        last_result="STOP attempts=3/3 — no-loop cap; auto-heal paused",
        age_seconds=60,
        fresh=True,
    )
    rep = build_status_report(**inp)
    daemon_row = next(r for r in rep.rows if r.label == "Heal daemon")
    assert daemon_row.status is RowStatus.DOWN
    assert rep.overall == "DEGRADED"


def test_daemon_disabled_token_is_attention_kill_switch() -> None:
    # The DISABLED kill-switch is an INTENTIONAL off (fail-safe), not a wedge — it
    # is at-risk (no protection while off) but deliberate, so surface ATTENTION.
    inp = _all_green_inputs()
    inp["daemon"] = DaemonInfo(
        loaded=True,
        last_result="DISABLED kill-switch present — no-op",
        age_seconds=60,
        fresh=True,
    )
    rep = build_status_report(**inp)
    daemon_row = next(r for r in rep.rows if r.label == "Heal daemon")
    assert daemon_row.status is RowStatus.ATTENTION
    assert rep.overall == "ATTENTION"
