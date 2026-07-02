"""sanctum onboard — network-resilience gate: registration, dispatch, skippability,
never-strand + honest-verify, DHCP-not-static, and the heal-daemon install.

The network-resilience gate is the onboard-time front door to ``sanctum net heal``:
it reads the node's live L3 posture (``heal.probe_posture`` → ``heal.diagnose_posture``)
and, on a STATIC_DRIFT verdict (a pinned Manual address that strands the node on any
foreign LAN), offers the GUIDED DHCP flip — but only after confirming the never-strand
spine (Tailscale tailnet / TB5) is alive, so a failed flip is always reachable
out-of-band. It then installs the self-healing LaunchDaemon so the node keeps healing
after onboarding. HONEST-VERIFY: the green check is derived from a REAL re-probe that
shows DHCP-not-static, never from "the step ran"; an UNVERIFIED probe configures
nothing (fail-closed), and a DOUBLE_NAT_OVERLAP verdict is alert-only (stays out of
the NAT domain, never mutates).

These tests lock the contracts a reviewer most wants pinned, mirroring the
``wifi-identity`` gate that is its sibling in the same "Your Network" chapter:

1. The gate is registered (``RECIPE_GATES``/``_CHAPTER_GATES``/``_GATE_LABELS``),
   dispatched (``_run_gate``), and skippable (``--yes`` short-circuits before probe).
2. NEVER-STRAND / HONEST-VERIFY: the flip fires only with a live spine + a real
   re-probe that reads DHCP; an UNVERIFIED / spine-down / declined path mutates
   nothing and returns False.
3. STAYS-OUT-OF-NAT: a DOUBLE_NAT_OVERLAP verdict is alert-only — never flips.

Every network read + mutation is a module-level seam (``heal.probe_posture`` /
``_flip_to_dhcp`` / ``_install_net_heal_daemon``) the tests patch, so no live
networksetup / ipconfig / launchctl / socket is ever touched.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import patch

import pytest

from sanctum_cli import recipes
from sanctum_cli.commands import onboard
from sanctum_cli.net import heal

# ── helpers: deterministic postures without touching hardware ────────────


def _posture(**kw: Any) -> heal.NetPosture:
    """A healthy DHCP posture; ``**kw`` overrides fields for the case under test."""
    base = dict(
        iface="en1",
        config_method="DHCP",
        ip="10.0.0.10",
        subnet="255.255.255.0",
        gateway="10.0.0.1",
        gateway_reachable=True,
        associated=True,
        on_tailnet=True,
        tb5_up=True,
    )
    base.update(kw)
    return heal.NetPosture(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_live_net(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every net-heal seam to a conservative / fail-closed value so an
    un-stubbed test can NEVER touch the live network / launchctl.

    ``probe_posture`` → a healthy DHCP posture (nothing to do); the flip + the daemon
    install are no-ops that never shell out. Tests that exercise a path re-stub the
    relevant seam.
    """
    monkeypatch.setattr(heal, "probe_posture", lambda *a, **k: _posture())
    monkeypatch.setattr(onboard, "_flip_to_dhcp", lambda: None)
    monkeypatch.setattr(onboard, "_install_net_heal_daemon", lambda: None)


# ── 1. Gate registered + dispatched + skippable ──────────────────────────


def test_network_resilience_gate_registered_and_runs() -> None:
    """Registered in the chapter + every recipe, and dispatched."""
    assert "network-resilience" in onboard._CHAPTER_GATES["Your Network"]
    for r in ("family", "operator", "code"):
        assert "network-resilience" in onboard.RECIPE_GATES[r]
    with patch.object(onboard, "_run_network_resilience", return_value=True) as g:
        assert onboard._run_gate("network-resilience", yes=True) is True
        g.assert_called_once()


def test_gate_registered_data_references_real_recipes() -> None:
    """Every recipe named in RECIPE_GATES is a real built-in; label present."""
    assert set(onboard.RECIPE_GATES) <= set(recipes.BUILTINS)
    assert "network-resilience" in onboard._GATE_LABELS


def test_gate_wired_into_dispatch_loop() -> None:
    """The 'network-resilience' branch is actually dispatched — registration alone is not enough."""
    src = inspect.getsource(onboard._run_gate)
    assert 'gate == "network-resilience"' in src
    assert "_run_network_resilience(yes=yes)" in src


def test_gate_additive_ordering_after_wifi_identity() -> None:
    """Additive: network-resilience sits AFTER wifi-identity in the family recipe."""
    gates = onboard.RECIPE_GATES["family"]
    assert gates.index("network-resilience") > gates.index("wifi-identity")


def test_gate_skipped_under_yes_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--yes`` SKIPS the gate before any probe/flip/install."""
    calls = {"probe": 0, "flip": 0, "install": 0}
    monkeypatch.setattr(
        heal, "probe_posture", lambda *a, **k: calls.__setitem__("probe", calls["probe"] + 1)
    )
    monkeypatch.setattr(
        onboard, "_flip_to_dhcp", lambda: calls.__setitem__("flip", calls["flip"] + 1)
    )
    monkeypatch.setattr(
        onboard,
        "_install_net_heal_daemon",
        lambda: calls.__setitem__("install", calls["install"] + 1),
    )
    assert onboard._run_network_resilience(yes=True) is False
    assert calls == {"probe": 0, "flip": 0, "install": 0}


# ── 2. NEVER-STRAND / HONEST-VERIFY / fail-closed ────────────────────────


def test_unverified_posture_configures_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A posture we cannot read (UNVERIFIED) → fail-closed, no flip, no install, False."""
    monkeypatch.setattr(
        heal, "probe_posture", lambda *a, **k: _posture(iface="", config_method="")
    )
    flipped: list[int] = []
    installed: list[int] = []
    monkeypatch.setattr(onboard, "_flip_to_dhcp", lambda: flipped.append(1))
    monkeypatch.setattr(onboard, "_install_net_heal_daemon", lambda: installed.append(1))
    assert onboard._run_network_resilience(yes=False) is False
    assert flipped == [] and installed == []


def test_healthy_dhcp_installs_daemon_no_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already DHCP + healthy → no flip; the heal daemon is still installed (True)."""
    monkeypatch.setattr(heal, "probe_posture", lambda *a, **k: _posture())
    flipped: list[int] = []
    installed: list[int] = []
    monkeypatch.setattr(onboard, "_flip_to_dhcp", lambda: flipped.append(1))
    monkeypatch.setattr(onboard, "_install_net_heal_daemon", lambda: installed.append(1))
    assert onboard._run_network_resilience(yes=False) is True
    assert flipped == []  # nothing to heal
    assert installed == [1]  # the daemon keeps it healthy going forward


def test_static_drift_flips_after_confirm_and_reprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """STATIC_DRIFT + live spine + confirm → flip; the green check is from a REAL re-probe."""
    postures = iter([_posture(config_method="Manual"), _posture(config_method="DHCP")])
    monkeypatch.setattr(heal, "probe_posture", lambda *a, **k: next(postures))
    monkeypatch.setattr(onboard.Confirm, "ask", staticmethod(lambda *a, **k: True))
    flipped: list[int] = []
    installed: list[int] = []
    monkeypatch.setattr(onboard, "_flip_to_dhcp", lambda: flipped.append(1))
    monkeypatch.setattr(onboard, "_install_net_heal_daemon", lambda: installed.append(1))
    assert onboard._run_network_resilience(yes=False) is True
    assert flipped == [1]  # the guided flip fired once
    assert installed == [1]


def test_static_drift_declined_no_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    """STATIC_DRIFT but the operator declines the flip → nothing mutated, False."""
    monkeypatch.setattr(heal, "probe_posture", lambda *a, **k: _posture(config_method="Manual"))
    monkeypatch.setattr(onboard.Confirm, "ask", staticmethod(lambda *a, **k: False))
    flipped: list[int] = []
    installed: list[int] = []
    monkeypatch.setattr(onboard, "_flip_to_dhcp", lambda: flipped.append(1))
    monkeypatch.setattr(onboard, "_install_net_heal_daemon", lambda: installed.append(1))
    assert onboard._run_network_resilience(yes=False) is False
    assert flipped == []  # declined → never flipped


def test_static_drift_spine_down_never_strands(monkeypatch: pytest.MonkeyPatch) -> None:
    """STATIC_DRIFT but NO spine (no tailnet, no TB5) → refuse to flip (never-strand), False."""
    monkeypatch.setattr(
        heal,
        "probe_posture",
        lambda *a, **k: _posture(config_method="Manual", on_tailnet=False, tb5_up=False),
    )
    # A stray Confirm must not be reached (the spine gate stops us before prompting).
    monkeypatch.setattr(
        onboard.Confirm, "ask", staticmethod(lambda *a, **k: pytest.fail("should not prompt"))
    )
    flipped: list[int] = []
    monkeypatch.setattr(onboard, "_flip_to_dhcp", lambda: flipped.append(1))
    assert onboard._run_network_resilience(yes=False) is False
    assert flipped == []  # never touch the interface with no out-of-band path


def test_static_drift_reprobe_still_manual_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flip fired but the re-probe still reads Manual → honest False (no false 'healed')."""
    postures = iter([_posture(config_method="Manual"), _posture(config_method="Manual")])
    monkeypatch.setattr(heal, "probe_posture", lambda *a, **k: next(postures))
    monkeypatch.setattr(onboard.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(onboard, "_flip_to_dhcp", lambda: None)
    installed: list[int] = []
    monkeypatch.setattr(onboard, "_install_net_heal_daemon", lambda: installed.append(1))
    # DHCP-not-static was NOT achieved → the gate must not claim it configured.
    assert onboard._run_network_resilience(yes=False) is False
    assert installed == []  # never install off an unverified heal


# ── 3. STAYS-OUT-OF-NAT: DOUBLE_NAT_OVERLAP is alert-only ────────────────


def test_double_nat_overlap_alerts_never_flips(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOUBLE_NAT_OVERLAP (router/NAT problem) → alert only, never flips (stays out of NAT)."""
    monkeypatch.setattr(
        heal,
        "probe_posture",
        lambda *a, **k: _posture(gateway_reachable=False),
    )
    monkeypatch.setattr(heal, "overlap_for", lambda posture: True)
    monkeypatch.setattr(
        onboard.Confirm, "ask", staticmethod(lambda *a, **k: pytest.fail("should not prompt"))
    )
    flipped: list[int] = []
    monkeypatch.setattr(onboard, "_flip_to_dhcp", lambda: flipped.append(1))
    assert onboard._run_network_resilience(yes=False) is False
    assert flipped == []  # we never touch the NAT domain
