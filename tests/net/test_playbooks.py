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
