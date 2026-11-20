"""Export hive-resolved hosts into council-router style node overlays.

Does not rewrite agent lists — only injects ``host`` from live Tailscale
resolve for named hive nodes, killing drift-prone hard-coded 100.x literals.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sanctum_cli.hive.sot import primary_node_name


def overlay_hosts(
    council_nodes: Mapping[str, Any],
    hive_nodes: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, str],
    *,
    mapping: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a deep-copied council nodes doc with hosts overlaid.

    ``mapping`` maps council node id → hive roster name, e.g.
    ``{"satellite": "studio", "mobile": "mbp"}``.
    Default mapping resolves ``hub`` from the primary hub node and the first
    ``satellite`` / ``mobile`` nodes present in ``hive_nodes``.
    """
    default_map: dict[str, str] = {}
    primary = primary_node_name(hive_nodes)
    if primary:
        default_map["hub"] = primary
    for name, node in hive_nodes.items():
        if not isinstance(node, Mapping):
            continue
        ntype = str(node.get("type") or "").strip().lower()
        if ntype in {"satellite", "mobile"} and ntype not in default_map:
            default_map[ntype] = name
    m = dict(default_map)
    if mapping:
        m.update(mapping)

    out: dict[str, Any] = json.loads(json.dumps(council_nodes))  # deep copy via JSON
    nodes = out.get("nodes")
    if not isinstance(nodes, dict):
        return out
    for council_id, block in nodes.items():
        if not isinstance(block, dict):
            continue
        hive_name = m.get(str(council_id), str(council_id))
        if hive_name not in hive_nodes and hive_name not in resolved:
            continue
        addr = resolved.get(hive_name)
        if not addr:
            continue
        block["host"] = addr
        block["_host_source"] = f"hive-resolve:{hive_name}"
        # Drop stale drift comments once resolved
        block.pop("_ip-allow_host", None)
    return out
