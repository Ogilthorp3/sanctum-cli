"""Tailnet peer identity — which tailnet machine may answer for a hive name.

``tailscale status --json`` lists this machine (``Self``) and every peer, each
with two names:

* ``DNSName`` — the MagicDNS name the control plane assigned
  (``home.tailnet-abc.ts.net.``). The control plane keeps it unique in the
  tailnet: a second machine that asks for a taken name gets ``home-1``.
* ``HostName`` — the OS host name the machine reports for itself. Any machine
  can set it to anything, another machine's name included.

So a machine is found by its MagicDNS stem only (:func:`parse_tailscale_peer_ips`),
and :func:`peer_refusal` adds what the roster knows: a stem two machines claim
answers for nobody, and a machine tagged ``tag:satellite`` never answers for a
node the roster says is not a satellite. Both ``node resolve`` (and everything
built on it: ``vault-host``, ``route``, ``export-council``) and the routing
online map (:mod:`sanctum_cli.hive.route`) apply the same rule.

A parsed record (one per stem)::

    online      bool       PeerStatus.Online (this machine: True)
    ips         list[str]  TailscaleIPs
    tags        list[str]  ACL tags (Tags), e.g. ["tag:satellite"]
    stable_id   str        the StableNodeID (PeerStatus.ID), "" when absent
    dns_name    str        MagicDNS name without the trailing dot
    host_name   str        self-reported HostName: for display, never a key
    self        bool       this machine's own record
    ambiguous   bool       only on a stem two or more peers claim; such a record
                           is offline with no IPs, and ``claims`` lists the peers
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sanctum_cli.hive.capabilities import NodeCapability, capabilities_of

#: The ACL tag a satellite carries (tailnet-toolkit acl.hujson ``tagOwners``).
SATELLITE_TAG = "tag:satellite"


def _dns_name(node: Mapping[str, Any]) -> str:
    raw = node.get("DNSName")
    return raw.strip().lower().rstrip(".") if isinstance(raw, str) else ""


def _magicdns_suffix(data: Mapping[str, Any], self_dns: str) -> str:
    """This tailnet's MagicDNS suffix (``tailnet-abc.ts.net``); "" when unknown."""
    current = data.get("CurrentTailnet")
    for raw in (
        data.get("MagicDNSSuffix"),
        current.get("MagicDNSSuffix") if isinstance(current, Mapping) else None,
    ):
        if isinstance(raw, str) and raw.strip(" ."):
            return raw.strip(" .").lower()
    # A status without the field: this machine's own name is <stem>.<suffix>.
    return self_dns.partition(".")[2]


def _stem(dns_name: str, suffix: str) -> str:
    """The short name ``dns_name`` has in this tailnet; "" when it has none.

    Under a known suffix only a direct child counts, so a machine shared in from
    another tailnet (``home.someone-else.ts.net``) has no short name here.
    """
    if suffix:
        if not dns_name.endswith("." + suffix):
            return ""
        stem = dns_name[: -len(suffix) - 1]
    else:
        stem = dns_name.partition(".")[0]
    return stem if stem and "." not in stem else ""


def _record(node: Mapping[str, Any], *, is_self: bool) -> dict[str, Any]:
    ips = node.get("TailscaleIPs")
    tags = node.get("Tags")
    stable_id = node.get("ID") or node.get("StableID")
    host_name = node.get("HostName")
    return {
        "online": True if is_self else bool(node.get("Online")),
        "ips": [str(i) for i in ips if i] if isinstance(ips, list) else [],
        "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
        "stable_id": stable_id if isinstance(stable_id, str) else "",
        "dns_name": _dns_name(node),
        "host_name": host_name.strip() if isinstance(host_name, str) else "",
        "self": is_self,
    }


def parse_tailscale_peer_ips(status_json: str) -> dict[str, dict[str, Any]]:
    """Map each MagicDNS stem to its machine, from ``tailscale status --json``.

    Pure parser (tests inject JSON). Keys are lowercased MagicDNS stems
    (``home``, ``studio``); a machine's self-reported ``HostName`` is never a
    key, and a machine without a MagicDNS name in this tailnet gets no entry.
    This machine's record is authoritative for its own stem: no peer replaces
    it. When two or more peers claim one stem, the stem maps to an ``ambiguous``
    record (offline, no IPs) whatever their online state, so nothing picks one.
    See the module docstring for the record.
    """
    try:
        data = json.loads(status_json or "{}")
    except (ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    self_node = data.get("Self")
    if not isinstance(self_node, dict):
        self_node = {}
    self_dns = _dns_name(self_node)
    suffix = _magicdns_suffix(data, self_dns)

    out: dict[str, dict[str, Any]] = {}
    self_stem = _stem(self_dns, suffix)
    if self_stem:
        out[self_stem] = _record(self_node, is_self=True)

    claims: dict[str, list[dict[str, Any]]] = {}
    peers = data.get("Peer")
    for peer in peers.values() if isinstance(peers, dict) else ():
        if not isinstance(peer, dict):
            continue
        stem = _stem(_dns_name(peer), suffix)
        if stem and stem != self_stem:
            claims.setdefault(stem, []).append(_record(peer, is_self=False))
    for stem, records in claims.items():
        if len(records) == 1:
            out[stem] = records[0]
        else:
            out[stem] = {
                "online": False,
                "ips": [],
                "tags": [],
                "stable_id": "",
                "dns_name": "",
                "host_name": "",
                "self": False,
                "ambiguous": True,
                "claims": records,
            }
    return out


def is_ip_literal(text: str) -> bool:
    """True when ``text`` is an IP address: an address, never a tailnet name.

    A dotted quad Python refuses (``010.0.0.5``) still counts as one.
    """
    value = text.strip()
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value))
    return True


@dataclass(frozen=True)
class PeerRefusal:
    """Why a tailnet record may not answer for a roster node: the error and its fix."""

    error: str
    fix: str


def _describe(record: Mapping[str, Any]) -> str:
    state = "online" if record.get("online") else "offline"
    return f"{record.get('dns_name') or '?'}, ID {record.get('stable_id') or '?'}, {state}"


def _not_a_satellite(
    name: str, node: Mapping[str, Any], nodes: Mapping[str, Mapping[str, Any]]
) -> str:
    """Where the roster says ``name`` is not a satellite; "" when it may be one.

    A declared ``type`` decides. A node without one is still a hub when another
    node syncs to it (``sync.hub``) or when it offers ``vault_authority``.
    """
    ntype = str(node.get("type") or "").strip().lower()
    if ntype:
        return "" if ntype == "satellite" else f"type: {ntype} in instance.yaml"
    for other, block in nodes.items():
        sync = block.get("sync") if isinstance(block, Mapping) else None
        hub = str(sync.get("hub") or "").strip().lower() if isinstance(sync, Mapping) else ""
        if other != name and hub == name.lower():
            return f"{other} syncs to it as its hub in instance.yaml"
    if NodeCapability.VAULT_AUTHORITY in capabilities_of(node):
        return "it offers vault_authority in instance.yaml"
    return ""


def peer_refusal(
    name: str,
    node: Mapping[str, Any],
    nodes: Mapping[str, Mapping[str, Any]],
    peer: Mapping[str, Any],
    stem: str,
) -> PeerRefusal | None:
    """Why the record found under ``stem`` may not answer for roster node ``name``.

    ``None`` when it may. Refused: an ambiguous stem (two or more machines claim
    it), and a machine tagged ``tag:satellite`` for a node that is not a
    satellite (see :func:`_not_a_satellite`). The caller fails closed: no other
    name, ``tailscale ip`` lookup or configured host is tried after a refusal.
    """
    if peer.get("ambiguous"):
        raw_claims = peer.get("claims")
        claimants = (
            [c for c in raw_claims if isinstance(c, Mapping)]
            if isinstance(raw_claims, list)
            else []
        )
        listed = "; ".join(_describe(c) for c in claimants) or "unknown machines"
        return PeerRefusal(
            error=(
                f"ambiguous tailnet name {stem!r}: {len(claimants)} tailnet machines claim it "
                f"({listed}) — refusing to pick one"
            ),
            fix=(
                "one MagicDNS name belongs to one machine: check `tailscale status --json`, "
                "then remove or rename the extra machine in the Tailscale admin console"
            ),
        )
    tags = peer.get("tags") or ()
    if isinstance(tags, str):
        tags = (tags,)  # one tag, not one per letter
    if any(str(t).strip().lower() == SATELLITE_TAG for t in tags):
        where = _not_a_satellite(name, node, nodes)
        if where:
            return PeerRefusal(
                error=(
                    f"node {name!r} is not a satellite ({where}), but tailnet name {stem!r} "
                    f"belongs to a machine tagged {SATELLITE_TAG} ({_describe(peer)}) — "
                    "refusing to resolve it to a satellite"
                ),
                fix=(
                    f"if that machine really is {name!r}, correct its tags in the Tailscale "
                    "admin console or its entry in instance.yaml; if not, treat that "
                    "satellite as compromised"
                ),
            )
    return None
