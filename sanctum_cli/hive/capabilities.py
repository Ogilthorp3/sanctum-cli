"""Haus-node capabilities — what a hive member can offer.

Static declares in ``instance.yaml`` ``nodes.<name>.capabilities`` win.
If omitted, defaults are inferred from ``type`` + ``tier`` so a minimal
roster still routes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping


class NodeCapability(StrEnum):
    """Positive, routable offers on a haus node (not gear-device capabilities)."""

    VAULT_AUTHORITY = "vault_authority"
    INFERENCE = "inference"
    INFERENCE_HEAVY = "inference_heavy"
    LOCAL_INFERENCE = "local_inference"
    HA_SITE = "ha_site"
    TRAIN = "train"
    MESH_SEED = "mesh_seed"
    SATELLITE = "satellite"
    SCREEN_TIME = "screen_time"
    COUNCIL = "council"


# Accept common short names / typos from operators and agents.
CAPABILITY_ALIASES: dict[str, NodeCapability] = {
    "vault": NodeCapability.VAULT_AUTHORITY,
    "vault_authority": NodeCapability.VAULT_AUTHORITY,
    "memory": NodeCapability.VAULT_AUTHORITY,
    "inference": NodeCapability.INFERENCE,
    "infer": NodeCapability.INFERENCE,
    "mlx": NodeCapability.INFERENCE,
    "heavy": NodeCapability.INFERENCE_HEAVY,
    "inference_heavy": NodeCapability.INFERENCE_HEAVY,
    "cathedral": NodeCapability.INFERENCE_HEAVY,
    "local": NodeCapability.LOCAL_INFERENCE,
    "local_inference": NodeCapability.LOCAL_INFERENCE,
    "ahsoka_brain": NodeCapability.LOCAL_INFERENCE,
    "ha": NodeCapability.HA_SITE,
    "ha_site": NodeCapability.HA_SITE,
    "homeassistant": NodeCapability.HA_SITE,
    "train": NodeCapability.TRAIN,
    "training": NodeCapability.TRAIN,
    "mesh": NodeCapability.MESH_SEED,
    "mesh_seed": NodeCapability.MESH_SEED,
    "satellite": NodeCapability.SATELLITE,
    "screen_time": NodeCapability.SCREEN_TIME,
    "council": NodeCapability.COUNCIL,
}


DEFAULT_CAPABILITIES: dict[tuple[str, str], frozenset[NodeCapability]] = {
    # (type, tier) → defaults when capabilities: omitted
    ("hub", "primary"): frozenset(
        {
            NodeCapability.VAULT_AUTHORITY,
            NodeCapability.INFERENCE,
            NodeCapability.INFERENCE_HEAVY,
            NodeCapability.HA_SITE,
            NodeCapability.TRAIN,
            NodeCapability.MESH_SEED,
            NodeCapability.COUNCIL,
            NodeCapability.SCREEN_TIME,
        }
    ),
    ("hub", "peer"): frozenset(
        {
            NodeCapability.INFERENCE,
            NodeCapability.INFERENCE_HEAVY,
            NodeCapability.HA_SITE,
            NodeCapability.TRAIN,
            NodeCapability.MESH_SEED,
            NodeCapability.COUNCIL,
        }
    ),
    ("satellite", "edge"): frozenset(
        {
            NodeCapability.LOCAL_INFERENCE,
            NodeCapability.SATELLITE,
        }
    ),
    ("mobile", "edge"): frozenset(
        {
            NodeCapability.LOCAL_INFERENCE,
        }
    ),
    ("sensor", "edge"): frozenset(),
}


def normalize_capability(raw: str) -> NodeCapability | None:
    """Map a free-form string to :class:`NodeCapability`, or None if unknown."""
    key = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    if key in CAPABILITY_ALIASES:
        return CAPABILITY_ALIASES[key]
    try:
        return NodeCapability(key)
    except ValueError:
        return None


def capabilities_of(node: Mapping[str, Any] | None) -> frozenset[NodeCapability]:
    """Effective capability set for a roster node block."""
    if not node:
        return frozenset()
    raw = node.get("capabilities")
    if isinstance(raw, list) and raw:
        out: set[NodeCapability] = set()
        for item in raw:
            cap = normalize_capability(str(item))
            if cap is not None:
                out.add(cap)
        return frozenset(out)
    ntype = str(node.get("type") or "satellite").lower()
    tier = str(node.get("tier") or "edge").lower()
    if (ntype, tier) in DEFAULT_CAPABILITIES:
        return DEFAULT_CAPABILITIES[(ntype, tier)]
    # type-only fallback
    for (t, _tier), caps in DEFAULT_CAPABILITIES.items():
        if t == ntype:
            return caps
    return frozenset()
