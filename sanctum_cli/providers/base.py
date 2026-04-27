"""Provider abstraction.

A Provider is a pluggable model backend. The router picks one per
request based on intent, attachments, and config; the chosen Provider
handles the actual chat/vision/code call.

The interface is **synchronous and generator-based** in v0.2 — each
``chat()`` returns an iterator of text chunks. Async streaming is on
the v1.0 roadmap once the surface is stable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Flag, auto
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator
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
    """Token counts for a single completed request."""

    tokens_in: int
    tokens_out: int


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in a chat. v0.2 keeps the structure even though we only
    send single-turn requests — multi-turn lands in v0.3 without changing
    the interface."""

    role: Literal["user", "assistant", "system"]
    content: str


@dataclass(frozen=True, slots=True)
class ChatOpts:
    """Per-call dispatch options."""

    stream: bool = True
    max_tokens: int | None = None
    temperature: float | None = None


class Provider(ABC):
    """Interface every provider must implement.

    The router treats Provider instances as opaque. Capability gating
    happens before dispatch so a provider that lacks ``VISION`` is
    never asked to handle an image.
    """

    name: str
    capabilities: Capability

    @abstractmethod
    def chat(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
        """Yield response text chunks. Returns immediately if ``opts.stream``
        is False — yields a single full chunk in that case so callers don't
        branch on streaming mode."""

    @abstractmethod
    def health(self) -> HealthSnapshot:
        """Single round-trip probe. Bounded by the provider's timeout config.

        Must never raise — failures populate ``ok=False`` with a ``detail``."""

    @abstractmethod
    def cost(self, usage: Usage) -> Decimal:
        """USD cost for a given usage, computed locally — no network call."""
