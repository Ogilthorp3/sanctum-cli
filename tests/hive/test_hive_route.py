"""Hive capability routing + SoT + council overlay — pure unit tests."""

from __future__ import annotations

from sanctum_cli.hive.capabilities import NodeCapability, capabilities_of, normalize_capability
from sanctum_cli.hive.export_council import overlay_hosts
from sanctum_cli.hive.route import pick_node
from sanctum_cli.hive.sot import peers_of, primary_node_name, validate_roster

ROSTER = {
    "manoir": {
        "type": "hub",
        "tier": "primary",
        "tailscale_name": "manoir",
        "capabilities": [
            "vault_authority",
            "inference",
            "inference_heavy",
            "ha_site",
        ],
    },
    "chalet": {
        "type": "satellite",
        "tier": "edge",
        "tailscale_name": "chalet",
        "capabilities": ["local_inference", "satellite"],
        "sync": {"hub": "manoir"},
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
        "aliases": ["berts-mbp"],
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
    assert primary_node_name(ROSTER) == "manoir"
    assert peers_of(ROSTER) == ["montreal"]
    assert validate_roster(ROSTER) == []


def test_validate_double_primary() -> None:
    bad = {
        "a": {"type": "hub", "tier": "primary"},
        "b": {"type": "hub", "tier": "primary"},
    }
    problems = validate_roster(bad)
    assert any("multiple primary" in p for p in problems)


def test_pick_local_inference_on_chalet() -> None:
    r = pick_node(
        "local_inference",
        ROSTER,
        prefer="local",
        local_name="chalet",
        peer_ips={"chalet": {"online": True, "ips": ["100.1.1.1"]}},
    )
    assert r["ok"] and r["node"] == "chalet"


def test_pick_vault_primary() -> None:
    r = pick_node("vault", ROSTER, prefer="primary")
    assert r["ok"] and r["node"] == "manoir"


def test_pick_heavy_forced_peer() -> None:
    r = pick_node(
        "inference_heavy",
        ROSTER,
        prefer="hub:montreal",
        peer_ips={
            "manoir": {"online": True, "ips": ["100.0.0.1"]},
            "montreal": {"online": True, "ips": ["100.0.0.2"]},
        },
    )
    assert r["ok"] and r["node"] == "montreal"


def test_pick_offline_forced_fails() -> None:
    r = pick_node(
        "inference",
        ROSTER,
        prefer="hub:montreal",
        peer_ips={"montreal": {"online": False, "ips": ["100.0.0.2"]}},
    )
    assert r["ok"] is False


def test_overlay_council_hosts() -> None:
    council = {
        "nodes": {
            "satellite": {"host": "100.112.203.32", "agents": ["ahsoka"]},
            "hub": {"host": "10.10.10.1", "agents": ["jocasta"]},
        }
    }
    resolved = {"chalet": "100.68.189.17", "manoir": "100.107.112.118"}
    out = overlay_hosts(council, ROSTER, resolved)
    assert out["nodes"]["satellite"]["host"] == "100.68.189.17"
    assert out["nodes"]["satellite"]["_host_source"] == "hive-resolve:chalet"
    assert out["nodes"]["hub"]["host"] == "100.107.112.118"
