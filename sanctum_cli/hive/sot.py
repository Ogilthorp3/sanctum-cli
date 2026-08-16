"""Source-of-truth rules for the hive roster."""

from __future__ import annotations

from typing import Any, Mapping

from sanctum_cli.hive.naming import validate_roster_naming


def primary_node_name(nodes: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Return the unique primary hub name, or None if missing/ambiguous."""
    primaries = [
        name
        for name, n in nodes.items()
        if str(n.get("type") or "").lower() == "hub"
        and str(n.get("tier") or "primary").lower() == "primary"
    ]
    if len(primaries) == 1:
        return primaries[0]
    if not primaries:
        # Legacy: single hub without tier
        hubs = [name for name, n in nodes.items() if str(n.get("type") or "").lower() == "hub"]
        return hubs[0] if len(hubs) == 1 else None
    return None  # ambiguous


def peers_of(nodes: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Names of hub nodes with ``tier: peer`` (sorted)."""
    return sorted(
        name
        for name, n in nodes.items()
        if str(n.get("type") or "").lower() == "hub"
        and str(n.get("tier") or "").lower() == "peer"
    )


def validate_roster(nodes: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Return human-readable problems (empty list = ok)."""
    problems: list[str] = []
    if not nodes:
        problems.append("roster empty — declare at least nodes.<primary-hub>")
        return problems
    primaries = [
        name
        for name, n in nodes.items()
        if str(n.get("type") or "").lower() == "hub"
        and str(n.get("tier") or "primary").lower() == "primary"
    ]
    bare_hubs = [
        name
        for name, n in nodes.items()
        if str(n.get("type") or "").lower() == "hub" and not n.get("tier")
    ]
    if len(primaries) > 1:
        problems.append(f"multiple primary hubs: {', '.join(sorted(primaries))}")
    if not primaries and not bare_hubs:
        problems.append("no primary hub (type: hub, tier: primary)")
    if len(primaries) == 0 and len(bare_hubs) > 1:
        problems.append(f"multiple hubs without tier: {', '.join(sorted(bare_hubs))}")
    for name, n in nodes.items():
        if str(n.get("type") or "").lower() == "satellite":
            sync = n.get("sync") if isinstance(n.get("sync"), dict) else {}
            hub = (sync or {}).get("hub")
            if hub and hub not in nodes:
                problems.append(f"{name}: sync.hub={hub!r} not in roster")
        if str(n.get("tier") or "").lower() == "peer" and str(n.get("type") or "").lower() != "hub":
            problems.append(f"{name}: tier peer requires type hub")
    problems.extend(validate_roster_naming(nodes))
    return problems
