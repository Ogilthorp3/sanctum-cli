"""Hive capability routing + SoT + council overlay — pure unit tests."""

from __future__ import annotations

from sanctum_cli.hive.capabilities import NodeCapability, capabilities_of, normalize_capability
from sanctum_cli.hive.export_council import overlay_hosts
from sanctum_cli.hive.route import pick_node
from sanctum_cli.hive.sot import peers_of, primary_node_name, validate_roster

ROSTER = {
    "home": {
        "type": "hub",
        "tier": "primary",
        "tailscale_name": "home",
        "capabilities": [
            "vault_authority",
            "inference",
            "inference_heavy",
            "ha_site",
        ],
    },
    "studio": {
        "type": "satellite",
        "tier": "edge",
        "tailscale_name": "studio",
        "capabilities": ["local_inference", "satellite"],
        "sync": {"hub": "home"},
    },
    "montreal": {
        "type": "hub",
        "tier": "peer",
        "tailscale_name": "montreal",
        "capabilities": ["inference", "inference_heavy", "train"],
    },
    "mbp": {
        "type": "mobile",
        "tier": "edge",
        "tailscale_name": "mbp",
        "aliases": ["alice-mbp"],
    },
}


def test_normalize_capability_aliases() -> None:
    assert normalize_capability("vault") is NodeCapability.VAULT_AUTHORITY
    assert normalize_capability("mlx") is NodeCapability.INFERENCE
    assert normalize_capability("nope") is None


def test_capabilities_defaults_for_mobile() -> None:
    caps = capabilities_of({"type": "mobile", "tier": "edge"})
    assert NodeCapability.LOCAL_INFERENCE in caps


def test_primary_and_peers() -> None:
    assert primary_node_name(ROSTER) == "home"
    assert peers_of(ROSTER) == ["montreal"]
    assert validate_roster(ROSTER) == []


def test_validate_double_primary() -> None:
    bad = {
        "a": {"type": "hub", "tier": "primary"},
        "b": {"type": "hub", "tier": "primary"},
    }
    problems = validate_roster(bad)
    assert any("multiple primary" in p for p in problems)


def test_pick_local_inference_on_studio() -> None:
    r = pick_node(
        "local_inference",
        ROSTER,
        prefer="local",
        local_name="studio",
        peer_ips={"studio": {"online": True, "ips": ["100.64.0.11"]}},
    )
    assert r["ok"] and r["node"] == "studio"


def test_pick_vault_primary() -> None:
    r = pick_node("vault", ROSTER, prefer="primary")
    assert r["ok"] and r["node"] == "home"


def test_pick_heavy_forced_peer() -> None:
    r = pick_node(
        "inference_heavy",
        ROSTER,
        prefer="hub:montreal",
        peer_ips={
            "home": {"online": True, "ips": ["100.64.0.1"]},
            "montreal": {"online": True, "ips": ["100.64.0.2"]},
        },
    )
    assert r["ok"] and r["node"] == "montreal"


def test_pick_offline_forced_fails() -> None:
    r = pick_node(
        "inference",
        ROSTER,
        prefer="hub:montreal",
        peer_ips={"montreal": {"online": False, "ips": ["100.64.0.2"]}},
    )
    assert r["ok"] is False


def test_a_record_that_cannot_answer_for_a_node_says_nothing_about_it() -> None:
    """An ambiguous MagicDNS name, or a satellite holding the hub's, is not the hub up."""
    ambiguous = {
        "online": False,
        "ips": [],
        "ambiguous": True,
        "claims": [
            {"dns_name": "home.t.ts.net", "stable_id": "nA", "online": True},
            {"dns_name": "home.t.ts.net", "stable_id": "nB", "online": False},
        ],
    }
    squatter = {"online": True, "ips": ["100.64.0.9"], "tags": ["tag:satellite"]}
    for peer in (ambiguous, squatter):
        r = pick_node("vault", ROSTER, prefer="primary", peer_ips={"home": peer})
        assert r["online"]["home"] is None
    # The satellite's own row still reads its record.
    r = pick_node(
        "satellite",
        ROSTER,
        peer_ips={"studio": {"online": True, "ips": ["100.64.0.11"], "tags": ["tag:satellite"]}},
    )
    assert r["online"]["studio"] is True


def test_overlay_council_hosts() -> None:
    council = {
        "nodes": {
            "satellite": {"host": "100.64.0.32", "agents": ["ahsoka"]},
            "hub": {"host": "10.10.10.1", "agents": ["jocasta"]},
        }
    }
    resolved = {"studio": "100.64.0.17", "home": "100.64.0.18"}
    out = overlay_hosts(council, ROSTER, resolved)
    assert out["nodes"]["satellite"]["host"] == "100.64.0.17"
    assert out["nodes"]["satellite"]["_host_source"] == "hive-resolve:studio"
    assert out["nodes"]["hub"]["host"] == "100.64.0.18"
