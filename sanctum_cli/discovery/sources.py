"""Passive candidate sources for haus discovery — each fails open.

A source NEVER raises: a missing binary, an unreadable table, or a parse miss
contributes nothing rather than breaking the scan. The scanner unions whatever
the sources return.
"""

from __future__ import annotations

import contextlib
import re
import socket
from typing import TYPE_CHECKING

from sanctum_cli.discovery.types import Candidate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from sanctum_cli.devices.base import Runner

__all__ = ["arp_cache", "router_clients", "ssdp"]

# "? (10.0.0.5) at 11:22:33:44:55:66 on en0 ..." — skip "(incomplete)".
_ARP_LINE = re.compile(r"\((?P<ip>\d+\.\d+\.\d+\.\d+)\) at (?P<mac>[0-9a-fA-F:]{11,17}) ")
_SSDP_LOCATION = re.compile(
    r"^location:\s*https?://(?P<ip>\d+\.\d+\.\d+\.\d+)", re.IGNORECASE | re.MULTILINE
)
_SSDP_ST = re.compile(r"^st:\s*(?P<st>\S+)", re.IGNORECASE | re.MULTILINE)
_SSDP_ADDR = ("239.255.255.250", 1900)
_SSDP_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\nST: ssdp:all\r\n\r\n"
)


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


def _default_ssdp_search(timeout: float = 1.5) -> list[str]:
    """One UDP M-SEARCH multicast; collect responses until ``timeout``.

    Pure stdlib — no dependency. Any socket error propagates to :func:`ssdp`,
    which suppresses it (fail-open).
    """
    responses: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(_SSDP_MSEARCH.encode("ascii"), _SSDP_ADDR)
        while True:
            try:
                data, _ = sock.recvfrom(2048)
            except TimeoutError:
                break
            responses.append(data.decode("utf-8", errors="replace"))
    finally:
        sock.close()
    return responses


def ssdp(*, search: Callable[[], list[str]] = _default_ssdp_search) -> Iterable[Candidate]:
    """Candidates from SSDP/UPnP M-SEARCH responses (routers/mesh advertise IGD)."""
    responses: list[str] = []
    with contextlib.suppress(Exception):
        responses = search()
    seen: dict[str, Candidate] = {}
    for resp in responses:
        loc = _SSDP_LOCATION.search(resp)
        if not loc:
            continue
        ip = loc.group("ip")
        st = _SSDP_ST.search(resp)
        hint = f"ssdp:{st.group('st')}" if st else "ssdp:rootdevice"
        cand = Candidate(ip=ip, hints=frozenset({hint}))
        seen[ip] = seen[ip].merge(cand) if ip in seen else cand
    return list(seen.values())


def router_clients(*, lister: Callable[[], list[Candidate]] | None) -> Iterable[Candidate]:
    """Candidates from a router/Firewalla client table, when a lister is wired.

    No paired provider exposes a client table yet, so ``lister`` is ``None`` in
    the MVP and this yields nothing. The seam is here so a provider that later
    lists DHCP leases drops in without touching the scanner. Fail-open.
    """
    if lister is None:
        return []
    out: list[Candidate] = []
    with contextlib.suppress(Exception):
        out = lister()
    return out
