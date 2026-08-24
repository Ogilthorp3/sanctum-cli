"""Hive naming contract — one name per machine, Apple-simple.

Product rule (Steve Jobs test):
  *What do you call it when you talk to it?* That string is the only name.

Layers (no metaphor bleed — see sanctum-docs operations/naming):

  place   manoir, chalet, montreal     physical site / haus
  brain   manoir, chalet               site Mini (roster key == .node_id == MagicDNS)
  mobile  mbp                          road machines (not a site)
  infra   {site}-fw, {site}-ha         Firewalla / HA Green on the tailnet
  agent   yoda, windu, ahsoka          Jedi seats (never a Tailscale host name)
  mesh    champion labels              open mesh identity — separate plane

Patterns
--------
  site brain:   ^[a-z][a-z0-9]{1,15}$          (place name, short)
  mobile:       ^[a-z][a-z0-9]{1,15}$          (mbp, not berts-mbp)
  site infra:   ^{site}-(fw|ha|orbi)$

Forbidden as addresses
----------------------
  * owner prefixes (berts-mbp)
  * memory-size codenames (MM64, MBP128)
  * bare LAN IPs for cross-site
  * dual identity (roster key ≠ preferred MagicDNS without an aliases: entry)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# Preferred roster keys for known gear roles (not exhaustive — convention).
INFRA_SUFFIXES = frozenset({"fw", "ha", "orbi"})
# Legacy Tailscale machine names → preferred hive name (transition table).
LEGACY_ALIASES: dict[str, str] = {
    "berts-mbp": "mbp",
    "berts-macbook-pro-m4-max-128gb": "mbp",
    "manoir-firewalla": "manoir-fw",
    "chalet-firewalla": "chalet-fw",
    "ts-firewalla": "manoir-fw",  # historical Gold Pro label
    "homeassistant": "manoir-ha",
    "manoir-ha": "manoir-ha",
}

_SITE_OR_MOBILE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
_INFRA = re.compile(r"^([a-z][a-z0-9]{1,15})-(fw|ha|orbi)$")
_MEMORY_CODE = re.compile(r"^(mm|mbp)\d+$", re.I)
_OWNER_PREFIX = re.compile(r"^(berts?|neo|admin)[-_]", re.I)


def is_valid_hive_name(name: str) -> bool:
    """True if ``name`` is a legal hive address (site brain, mobile, or site-infra)."""
    n = (name or "").strip().lower()
    if not n or "." in n or "/" in n:
        return False
    if _MEMORY_CODE.match(n) or _OWNER_PREFIX.match(n):
        return False
    if _SITE_OR_MOBILE.match(n):
        return True
    return bool(_INFRA.match(n))


def classify_name(name: str) -> str:
    """Return ``site_brain`` | ``mobile`` | ``site_infra`` | ``invalid``."""
    n = (name or "").strip().lower()
    if not is_valid_hive_name(n):
        return "invalid"
    m = _INFRA.match(n)
    if m:
        return "site_infra"
    # Convention: known mobiles (extend carefully)
    if n in {"mbp", "iphone", "ipad"}:
        return "mobile"
    return "site_brain"


def preferred_name(raw: str, nodes: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """Map a raw stem (Tailscale HostName / alias) to the preferred hive name."""
    n = (raw or "").strip().lower().split(".")[0]
    if not n:
        return n
    # Explicit aliases on roster rows
    if nodes:
        for key, block in nodes.items():
            if key.lower() == n:
                return key
            ts = str(block.get("tailscale_name") or "").strip().lower()
            if ts == n:
                return key
            for a in block.get("aliases") or []:
                if str(a).strip().lower() == n:
                    return key
    if n in LEGACY_ALIASES:
        return LEGACY_ALIASES[n]
    return n


def aliases_of(node: Mapping[str, Any] | None) -> list[str]:
    """All stems that should resolve to this node (tailscale_name + aliases)."""
    if not node:
        return []
    out: list[str] = []
    ts = str(node.get("tailscale_name") or "").strip().lower()
    if ts:
        out.append(ts)
    for a in node.get("aliases") or []:
        s = str(a).strip().lower()
        if s and s not in out:
            out.append(s)
    return out


def validate_node_naming(name: str, node: Mapping[str, Any]) -> list[str]:
    """Human-readable naming problems for one roster row (empty = ok)."""
    problems: list[str] = []
    key = (name or "").strip()
    if not is_valid_hive_name(key):
        problems.append(
            f"{key!r}: invalid hive name — use short place (manoir), mobile (mbp), "
            f"or site-infra (manoir-fw); no owner prefixes or MM64-style codes"
        )
        return problems
    ts = str(node.get("tailscale_name") or "").strip()
    # preferred tailscale_name should be perfect; legacy goes in aliases
    if (
        ts
        and not is_valid_hive_name(ts)
        and ts.lower() not in {a.lower() for a in (node.get("aliases") or [])}
        and (_OWNER_PREFIX.match(ts) or _MEMORY_CODE.match(ts))
    ):
        problems.append(
            f"{key}: tailscale_name={ts!r} is legacy form — set tailscale_name={key!r} "
            f"and put {ts!r} in aliases: until Tailscale is renamed"
        )
    if ts and ts.lower() != key.lower() and classify_name(key) != "site_infra":
        # brain/mobile: prefer key == tailscale_name
        aliases = {str(a).lower() for a in (node.get("aliases") or [])}
        if ts.lower() not in aliases and ts.lower() != key.lower():
            problems.append(
                f"{key}: tailscale_name={ts!r} differs from roster key — "
                f"either rename TS machine to {key!r} or list {ts!r} under aliases"
            )
    site = str(node.get("site") or "").strip().lower()
    if site and classify_name(key) == "site_brain" and site != key.lower():
        problems.append(
            f"{key}: site={site!r} should equal roster key for site brains "
            f"(one place name, one string)"
        )
    return problems


def validate_roster_naming(nodes: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """All naming problems across the roster."""
    out: list[str] = []
    for name, block in sorted(nodes.items()):
        if isinstance(block, dict):
            out.extend(validate_node_naming(name, block))
    # duplicate preferred stems
    seen: dict[str, str] = {}
    for name, block in nodes.items():
        if not isinstance(block, dict):
            continue
        for stem in [name.lower(), *aliases_of(block)]:
            if stem in seen and seen[stem] != name:
                out.append(f"stem {stem!r} claimed by both {seen[stem]!r} and {name!r}")
            else:
                seen[stem] = name
    return out


def suggest_infra_name(site: str, role: str) -> str:
    """``manoir`` + ``fw`` → ``manoir-fw``."""
    s = (site or "").strip().lower()
    r = (role or "").strip().lower()
    if r in {"firewalla", "firewall", "purple", "gold"}:
        r = "fw"
    if r in {"homeassistant", "home-assistant", "green"}:
        r = "ha"
    return f"{s}-{r}"
