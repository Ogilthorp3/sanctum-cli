"""Capability routing — pick which hive node should handle work."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sanctum_cli.hive.capabilities import NodeCapability, capabilities_of, normalize_capability
from sanctum_cli.hive.sot import primary_node_name

if TYPE_CHECKING:
    from collections.abc import Mapping


class RoutePrefer(StrEnum):
    LOCAL = "local"
    FASTEST = "fastest"
    PRIMARY = "primary"
    # hub:<name> handled as prefer string, not enum member


def _online_map(
    peer_ips: Mapping[str, Mapping[str, Any]],
    nodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, bool | None]:
    """node_name → True/False/None (unknown)."""
    out: dict[str, bool | None] = {}
    for name, n in nodes.items():
        ts = str(n.get("tailscale_name") or n.get("host") or name).strip().lower()
        stem = ts.split(".")[0]
        peer = peer_ips.get(stem) or peer_ips.get(name.lower())
        if peer is None:
            out[name] = None
        else:
            out[name] = bool(peer.get("online"))
    return out


def candidates_for(
    capability: NodeCapability | str,
    nodes: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Node names that offer ``capability`` (sorted)."""
    cap = (
        capability
        if isinstance(capability, NodeCapability)
        else normalize_capability(str(capability))
    )
    if cap is None:
        return []
    return sorted(name for name, n in nodes.items() if cap in capabilities_of(n))


def pick_node(
    capability: NodeCapability | str,
    nodes: Mapping[str, Mapping[str, Any]],
    *,
    prefer: str = "fastest",
    local_name: str | None = None,
    peer_ips: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Choose a node for ``capability``.

    ``prefer``:
      * ``local`` — this machine if it offers the cap, else fall through
      * ``fastest`` — online candidates; primary preferred among online hubs
      * ``primary`` — primary hub if it offers, else fail
      * ``hub:<name>`` — force that hub if it offers

    Returns a result dict: ok, node, reason, candidates, online.
    """
    cap = (
        capability
        if isinstance(capability, NodeCapability)
        else normalize_capability(str(capability))
    )
    if cap is None:
        return {
            "ok": False,
            "node": None,
            "capability": str(capability),
            "reason": f"unknown capability {capability!r}",
            "candidates": [],
            "online": {},
        }
    cands = candidates_for(cap, nodes)
    online = _online_map(peer_ips or {}, nodes)
    pref = (prefer or "fastest").strip().lower()

    def _result(node: str | None, reason: str, ok: bool = True) -> dict[str, Any]:
        return {
            "ok": ok and node is not None,
            "node": node,
            "capability": cap.value,
            "reason": reason,
            "candidates": cands,
            "online": {n: online.get(n) for n in cands},
        }

    if not cands:
        return _result(None, f"no node offers {cap.value}", ok=False)

    if pref.startswith("hub:"):
        forced = pref.split(":", 1)[1].strip()
        if forced in cands:
            if online.get(forced) is False:
                return _result(forced, f"forced hub:{forced} offers but is offline", ok=False)
            return _result(forced, f"prefer hub:{forced}")
        return _result(None, f"hub:{forced} does not offer {cap.value}", ok=False)

    if pref == "local" and local_name and local_name in cands:
        return _result(local_name, "prefer local (this node offers capability)")

    if pref == "primary":
        primary = primary_node_name(nodes)
        if primary and primary in cands:
            if online.get(primary) is False:
                return _result(primary, "primary offers but is offline", ok=False)
            return _result(primary, "prefer primary")
        return _result(None, "primary does not offer capability", ok=False)

    # fastest (default): online first, then primary, then local, then first cand
    online_cands = [n for n in cands if online.get(n) is True]
    pool = online_cands or [n for n in cands if online.get(n) is not False] or cands
    primary = primary_node_name(nodes)
    if primary and primary in pool:
        return _result(primary, "fastest → primary among available")
    if local_name and local_name in pool:
        return _result(local_name, "fastest → local among available")
    return _result(pool[0], f"fastest → {pool[0]}")


def route_capability(
    capability: str,
    nodes: Mapping[str, Mapping[str, Any]],
    *,
    prefer: str = "fastest",
    local_name: str | None = None,
    peer_ips: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Public alias for :func:`pick_node` with string capability."""
    return pick_node(
        capability,
        nodes,
        prefer=prefer,
        local_name=local_name,
        peer_ips=peer_ips,
    )
