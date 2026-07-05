"""Mesh discovery — find who is seeding a champion, degrading tracker -> DHT.

Discovery answers three questions for the mesh: *announce* that this haus seeds
an artifact, *find* who else seeds a given content hash, and list the current
*peers*. Each of those is served by an injected :class:`DiscoveryTransport`
seam, so the pure coordination logic is unit-tested with an in-memory fake; the
real transports (an HTTP tracker now, a DHT designed-for) live in
``sanctum_cli.mesh.adapters``.

:class:`Discovery` composes a ``primary`` transport with an optional
``fallback`` and **degrades primary -> fallback** — the Layer-1 shape is
tracker-primary, DHT-fallback. It degrades on two conditions:

* **error** — the primary transport raises (e.g. the tracker is unreachable);
* **empty** — the primary answers but has no hit (``find`` -> ``None`` /
  ``peers`` -> ``[]``), so a slower but wider transport gets a turn.

When the primary answers, the fallback is never consulted. With no fallback
configured, an empty answer is returned as-is and a primary error propagates —
Discovery never invents a result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sanctum_cli.mesh.types import ArtifactRef, ChampionManifest

__all__ = [
    "Discovery",
    "DiscoveryTransport",
]


class DiscoveryTransport(Protocol):
    """A pluggable discovery backend (HTTP tracker, DHT, or an in-memory fake).

    Transport-agnostic on purpose: seeders and addresses are opaque strings
    (typically tailnet IPs), a content hash is the ``"sha256:…"`` id from
    :func:`sanctum_cli.mesh.artifact.content_hash`, and ``find`` returns ``None``
    for a miss rather than raising. Any backend with this shape drops into
    :class:`Discovery`.
    """

    def announce(self, manifest: ChampionManifest, addr: str) -> None:
        """Advertise that ``addr`` seeds the artifact described by ``manifest``."""
        ...

    def find(self, content_hash: str) -> ArtifactRef | None:
        """Return the seeders + manifest for ``content_hash``, or ``None`` if unknown."""
        ...

    def peers(self) -> list[str]:
        """Return the addresses of currently-announced seeders."""
        ...


class Discovery:
    """Tracker-primary, DHT-fallback discovery that degrades on error or empty.

    ``primary`` and ``fallback`` are public so callers (and tests) can inspect or
    toggle them; ``fallback`` may be ``None`` for a single-transport deployment.
    """

    def __init__(
        self,
        primary: DiscoveryTransport,
        fallback: DiscoveryTransport | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    def announce(self, manifest: ChampionManifest, addr: str) -> None:
        """Announce on the primary, degrading to the fallback only on error.

        An announce has no "empty" outcome, so the fallback is used solely when
        the primary raises. With no fallback the error propagates.
        """
        fallback = self.fallback
        try:
            self.primary.announce(manifest, addr)
        except Exception:
            if fallback is None:
                raise
            fallback.announce(manifest, addr)

    def find(self, content_hash: str) -> ArtifactRef | None:
        """Find seeders for ``content_hash``, degrading to the fallback.

        Degrades when the primary raises *or* returns ``None`` (empty). With no
        fallback, a miss is ``None`` and a primary error propagates.
        """
        fallback = self.fallback
        if fallback is None:
            return self.primary.find(content_hash)
        try:
            ref = self.primary.find(content_hash)
        except Exception:
            return fallback.find(content_hash)
        return ref if ref is not None else fallback.find(content_hash)

    def peers(self) -> list[str]:
        """List seeders, degrading to the fallback on error or an empty list.

        With no fallback, the primary's answer (including ``[]``) is returned and
        a primary error propagates.
        """
        fallback = self.fallback
        if fallback is None:
            return self.primary.peers()
        try:
            found = self.primary.peers()
        except Exception:
            return fallback.peers()
        return found if found else fallback.peers()
