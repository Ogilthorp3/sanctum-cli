"""Assemble passive candidates + the gateway into a recognized HausInventory.

``discover_haus`` is pure orchestration over injected seams (``sources`` and a
``fingerprint`` callable), so it is unit-tested with fakes; the real wiring —
the ARP/SSDP sources and a fingerprint backed by the device registry — is built
by :func:`build_default_scan` and only exercised at the onboard boundary.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from sanctum_cli.devices.base import NetContext
from sanctum_cli.gear.types import Candidate, DiscoveredDevice, HausInventory

if TYPE_CHECKING:
    from sanctum_cli.devices.base import Runner

__all__ = ["Fingerprint", "Source", "build_default_scan", "discover_haus"]

Source = Callable[[], Iterable[Candidate]]
"""A passive candidate source (already bound to its runner/search seam)."""

Fingerprint = Callable[..., "tuple[str, str, float] | None"]
"""``(ip, *, runner) -> (kind, brand, score) | None`` — a recognized device or a miss."""


def discover_haus(
    net: NetContext,
    *,
    allow_active: bool,
    sources: list[Source],
    fingerprint: Fingerprint,
) -> HausInventory:
    """Union passive candidates with the gateway, fingerprint, and tally the rest.

    The gateway (``net.gateway_ip``) is ALWAYS fingerprinted — it is your own
    known-position gear. LAN candidates are fingerprinted only when
    ``allow_active`` (consent); without it they are counted as unrecognized, so
    a passive-only run still surfaces the gateway without probing strangers.
    """
    candidates: dict[str, Candidate] = {}
    if net.gateway_ip:
        candidates[net.gateway_ip] = Candidate(ip=net.gateway_ip, hints=frozenset({"gateway"}))
    for source in sources:
        items: Iterable[Candidate] = []
        with contextlib.suppress(Exception):
            items = list(source())
        for cand in items:
            candidates[cand.ip] = (
                candidates[cand.ip].merge(cand) if cand.ip in candidates else cand
            )

    devices: list[DiscoveredDevice] = []
    unrecognized = 0
    for ip, cand in candidates.items():
        is_gateway = "gateway" in cand.hints
        if not is_gateway and not allow_active:
            unrecognized += 1                      # no consent → don't probe a stranger
            continue
        hit: tuple[str, str, float] | None = None
        with contextlib.suppress(Exception):
            hit = fingerprint(ip, runner=net.runner)
        if hit is None:
            unrecognized += 1
            continue
        kind, brand, score = hit
        devices.append(
            DiscoveredDevice(kind=kind, brand=brand, ip=ip, name=cand.hostname or brand, score=score)
        )
    return HausInventory(devices=devices, unrecognized_count=unrecognized)


def build_default_scan(net: NetContext) -> HausInventory:  # pragma: no cover - wiring
    """Real wiring: ARP + SSDP sources + a registry-backed fingerprint.

    Thin glue exercised at the onboard boundary; the pure logic is
    :func:`discover_haus`, unit-tested above.
    """
    from sanctum_cli.commands import onboard  # for _NETWORK_GEAR_KINDS + eligible kinds
    from sanctum_cli.devices import registry
    from sanctum_cli.gear import sources

    def fingerprint(ip: str, *, runner: Runner | None) -> tuple[str, str, float] | None:
        probe_net = NetContext(gateway_ip=ip, runner=runner)
        for kind, _label in onboard._NETWORK_GEAR_KINDS:
            with contextlib.suppress(Exception):
                provider = registry.resolve(kind, probe_net)
                if type(provider).detect(probe_net) > 0:
                    return (kind, type(provider).brand, 1.0)
        return None

    return discover_haus(
        net,
        allow_active=True,
        sources=[
            lambda: sources.arp_cache(net.runner) if net.runner else [],
            lambda: sources.ssdp(),
        ],
        fingerprint=fingerprint,
    )
