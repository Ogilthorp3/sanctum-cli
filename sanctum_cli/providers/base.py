"""Provider abstraction.

A Provider is a pluggable model backend. The router picks one per
request based on intent, attachments, and config; the chosen Provider
handles the actual chat/vision/code call.

v0.1 ships the ABC + Capability enum only — no concrete implementations
yet. The shape is fixed here so v0.2 implementations and the router
share a stable interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Flag, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal


class Capability(Flag):
    """What a provider can do. Combined as bitflags."""

    NONE = 0
    CHAT = auto()
    VISION = auto()
    TOOLS = auto()
    STREAMING = auto()
    THINKING = auto()


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """One-shot view of a provider's reachability and quota."""

    ok: bool
    latency_ms: int | None
    quota_remaining: int | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class Usage:
    tokens_in: int
    tokens_out: int


class Provider(ABC):
    """Interface every provider must implement.

    The router treats Provider instances as opaque. Capability gating
    happens before dispatch so a provider that lacks ``VISION`` is
    never asked to handle an image.
    """

    name: str
    capabilities: Capability

    @abstractmethod
    async def health(self) -> HealthSnapshot:
        """Single round-trip probe. Bounded by the provider's timeout config."""

    @abstractmethod
    def cost(self, usage: Usage) -> Decimal:
        """USD cost for a given usage, computed locally — no network call."""
