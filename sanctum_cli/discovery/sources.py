"""Passive candidate sources for haus discovery — each fails open.

A source NEVER raises: a missing binary, an unreadable table, or a parse miss
contributes nothing rather than breaking the scan. The scanner unions whatever
the sources return.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING

from sanctum_cli.discovery.types import Candidate

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sanctum_cli.devices.base import Runner

__all__ = ["arp_cache"]

# "? (10.0.0.5) at 11:22:33:44:55:66 on en0 ..." — skip "(incomplete)".
_ARP_LINE = re.compile(r"\((?P<ip>\d+\.\d+\.\d+\.\d+)\) at (?P<mac>[0-9a-fA-F:]{11,17}) ")


def arp_cache(runner: Runner) -> Iterable[Candidate]:
    """Candidates from the local ARP table (`arp -a`) — hosts we've talked to."""
    out = ""
    with contextlib.suppress(Exception):
        out = runner(("arp", "-a"))
    seen: dict[str, Candidate] = {}
    for line in out.splitlines():
        m = _ARP_LINE.search(line)
        if not m:
            continue
        ip = m.group("ip")
        cand = Candidate(ip=ip, mac=m.group("mac").lower(), hints=frozenset({"arp"}))
        seen[ip] = seen[ip].merge(cand) if ip in seen else cand
    return list(seen.values())
