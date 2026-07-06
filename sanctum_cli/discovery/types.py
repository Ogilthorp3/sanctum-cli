"""Pure value types for haus hardware discovery."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Candidate", "DiscoveredDevice", "HausInventory"]


@dataclass(frozen=True)
class Candidate:
    """A host a passive source surfaced, before any fingerprint is attempted.

    ``hints`` are opaque source tags (e.g. an SSDP service type, ``"arp"``) that
    let the scanner score without an active probe; ``ip`` is the identity.
    """

    ip: str
    mac: str | None = None
    hostname: str | None = None
    hints: frozenset[str] = field(default_factory=frozenset)

    def merge(self, other: Candidate) -> Candidate:
        """Union two sightings of the same ``ip`` (first non-None field wins)."""
        return Candidate(
            ip=self.ip,
            mac=self.mac or other.mac,
            hostname=self.hostname or other.hostname,
            hints=self.hints | other.hints,
        )


@dataclass(frozen=True)
class DiscoveredDevice:
    """A candidate a provider fingerprint recognized as configurable gear."""

    kind: str
    brand: str
    ip: str
    name: str
    score: float


@dataclass(frozen=True)
class HausInventory:
    """The scan result: recognized devices + a count of everything else."""

    devices: list[DiscoveredDevice]
    unrecognized_count: int

    @property
    def recognized_count(self) -> int:
        return len(self.devices)
