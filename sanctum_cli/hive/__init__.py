"""Sanctum hive plane — node capabilities, routing, multi-hub SoT.

The LAN body (identity-rebind design map) is per-site. This package is the
nervous system: who can do what, where work should run, who is primary.
"""

from sanctum_cli.hive.capabilities import (
    CAPABILITY_ALIASES,
    DEFAULT_CAPABILITIES,
    NodeCapability,
    capabilities_of,
    normalize_capability,
)
from sanctum_cli.hive.naming import (
    LEGACY_ALIASES,
    is_valid_hive_name,
    preferred_name,
    suggest_infra_name,
    validate_roster_naming,
)
from sanctum_cli.hive.route import RoutePrefer, pick_node, route_capability
from sanctum_cli.hive.sot import peers_of, primary_node_name, validate_roster

__all__ = [
    "CAPABILITY_ALIASES",
    "DEFAULT_CAPABILITIES",
    "LEGACY_ALIASES",
    "NodeCapability",
    "RoutePrefer",
    "capabilities_of",
    "is_valid_hive_name",
    "normalize_capability",
    "peers_of",
    "pick_node",
    "preferred_name",
    "primary_node_name",
    "route_capability",
    "suggest_infra_name",
    "validate_roster",
    "validate_roster_naming",
]
