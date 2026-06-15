from __future__ import annotations

from sanctum_cli.net import playbooks
from sanctum_cli.net.types import Nat, Playbook


def test_builtins_present() -> None:
    assert {"bell", "generic", "cgnat"} <= set(playbooks.BUILTINS)


def test_every_playbook_has_required_fields_and_rollback() -> None:
    for pid, pb in playbooks.BUILTINS.items():
        assert isinstance(pb, Playbook)
        assert pb.id == pid
        assert pb.display_name
        assert pb.achieves in {"single_nat", "not_possible"}
        if pb.achieves == "single_nat":
            assert pb.steps, f"{pid} has no steps"
            assert pb.rollback, f"{pid} has no rollback"


def test_match_bell_by_gateway_ip() -> None:
    pb = playbooks.match(gateway_ip="192.168.2.1", http_title="", nat=Nat.DOUBLE)
    assert pb.id == "bell"


def test_match_bell_by_title() -> None:
    pb = playbooks.match(gateway_ip="10.9.9.9", http_title="Bell Giga Hub", nat=Nat.DOUBLE)
    assert pb.id == "bell"


def test_match_cgnat_overrides() -> None:
    pb = playbooks.match(gateway_ip=None, http_title="", nat=Nat.CGNAT)
    assert pb.id == "cgnat"


def test_match_falls_back_to_generic() -> None:
    pb = playbooks.match(gateway_ip="10.1.1.1", http_title="MysteryRouter", nat=Nat.DOUBLE)
    assert pb.id == "generic"


def test_detect_only_returns_known_playbook_ids() -> None:
    # The matcher must never return an id that isn't in BUILTINS (manifest contract).
    from sanctum_cli.net.types import Nat

    for nat in (Nat.SINGLE, Nat.DOUBLE, Nat.CGNAT, Nat.UNKNOWN):
        pb = playbooks.match(gateway_ip="203.0.113.1", http_title="whatever", nat=nat)
        assert pb.id in playbooks.BUILTINS


# ── Bell single-NAT field learnings (2026-06 cutover incident) ──────────────


def test_bell_playbook_encodes_path_mtu() -> None:
    bell = playbooks.BUILTINS["bell"]
    assert bell.mtu == 1492


def test_bell_playbook_has_pppoe_alternative() -> None:
    bell = playbooks.BUILTINS["bell"]
    assert bell.alt_playbook == "bell-pppoe"


def test_bell_playbook_precheck_warns_about_10x_lan() -> None:
    bell = playbooks.BUILTINS["bell"]
    assert bell.prechecks, "bell must carry a precheck for the Advanced-DMZ /1 trap"
    blob = " ".join(bell.prechecks).lower()
    assert "10." in blob
    assert "advanced dmz" in blob


def test_bell_playbook_gotcha_mentions_mtu_1492() -> None:
    bell = playbooks.BUILTINS["bell"]
    blob = " ".join(bell.gotchas).lower()
    assert "mtu" in blob
    assert "1492" in blob


def test_bell_playbook_gotcha_mentions_slash1_overlap() -> None:
    bell = playbooks.BUILTINS["bell"]
    blob = " ".join(bell.gotchas).lower()
    assert "/1" in blob or "128.0.0.0" in blob or "0.0.0.0/1" in blob


def test_bell_playbook_step_sets_mtu_1492() -> None:
    bell = playbooks.BUILTINS["bell"]
    blob = " ".join(bell.steps).lower()
    assert "1492" in blob


def test_bell_pppoe_in_builtins() -> None:
    assert "bell-pppoe" in playbooks.BUILTINS


def test_bell_pppoe_achieves_single_nat() -> None:
    assert playbooks.BUILTINS["bell-pppoe"].achieves == "single_nat"


def test_bell_pppoe_encodes_mtu() -> None:
    assert playbooks.BUILTINS["bell-pppoe"].mtu == 1492


def test_bell_pppoe_steps_mention_credentials_and_lan_port() -> None:
    pppoe = playbooks.BUILTINS["bell-pppoe"]
    blob = " ".join(pppoe.steps).lower()
    assert "pppoe" in blob
    assert "@bell.ca" in blob
    assert "lan port" in blob


def test_bell_pppoe_gotchas_cover_throughput_and_10x_kept() -> None:
    pppoe = playbooks.BUILTINS["bell-pppoe"]
    blob = " ".join(pppoe.gotchas).lower()
    assert "10." in blob  # LAN stays on 10.x
    assert "1.5" in blob or "gbps" in blob  # single-threaded throughput cap


def test_match_never_returns_bell_pppoe() -> None:
    # bell-pppoe is an ALTERNATIVE reached via bell.alt_playbook, never auto-matched.
    from sanctum_cli.net.types import Nat

    bell_pppoe = playbooks.BUILTINS["bell-pppoe"]
    probes = [
        ("192.168.2.1", "Bell Giga Hub", Nat.DOUBLE),
        ("10.0.0.1", "Bell", Nat.SINGLE),
        (None, "", Nat.CGNAT),
        ("203.0.113.1", "bell-pppoe", Nat.UNKNOWN),
    ]
    for gw, title, nat in probes:
        for gwip in (gw, *bell_pppoe.gateway_ips):
            pb = playbooks.match(gateway_ip=gwip, http_title=title, nat=nat)
            assert pb.id != "bell-pppoe"


def test_match_bell_still_wins_regression() -> None:
    # Adding bell-pppoe must not steal Bell's auto-match.
    from sanctum_cli.net.types import Nat

    assert playbooks.match(gateway_ip="192.168.2.1", http_title="", nat=Nat.DOUBLE).id == "bell"
    assert playbooks.match(gateway_ip=None, http_title="Bell Giga Hub", nat=Nat.DOUBLE).id == "bell"


def test_bell_alt_playbook_resolves_to_a_builtin() -> None:
    # The alt_playbook pointer must name a real BUILTINS entry (manifest contract).
    bell = playbooks.BUILTINS["bell"]
    assert bell.alt_playbook in playbooks.BUILTINS
