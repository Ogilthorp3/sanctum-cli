"""Unit tests for mesh discovery (tracker primary + DHT-fallback wrapper).

Covers the Task 4 guarantees, driven entirely by an in-memory fake transport
(the real HTTP tracker + DHT transports live in adapters):

* ``announce(manifest, addr)`` then ``find(hash)`` returns the ``ArtifactRef``
  carrying the announced seeder; ``peers()`` lists the announced nodes.
* ``Discovery`` degrades ``primary -> fallback`` on **error** (primary raises)
  and on **empty** (primary has no hit), and never consults the fallback when
  the primary answers.
* Without a fallback, an absent hash yields ``None`` and a primary transport
  error propagates (Discovery cannot invent a result).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sanctum_cli.mesh.discovery import Discovery
from sanctum_cli.mesh.types import ArtifactKind, ArtifactRef, ChampionManifest

if TYPE_CHECKING:
    from collections.abc import Mapping


def _manifest(content_hash: str) -> ChampionManifest:
    """A minimal well-formed manifest keyed by ``content_hash`` for the fakes."""
    return ChampionManifest(
        content_hash=content_hash,
        kind=ArtifactKind.LORA_ADAPTER,
        base_model="qwen3.6-35b-a3b-4bit",
        eval_scores={"tiered": 0.897},
        size_bytes=42_000_000,
        producer_pubkey="mldsa:PUB",
        signature="sig:XYZ",
    )


class FakeTransport:
    """In-memory ``DiscoveryTransport`` fake for both tracker and DHT roles.

    ``fail=True`` makes every method raise (exercises error-degradation).
    ``seeded`` pre-loads ``{content_hash: [addr, ...]}`` so a fallback can already
    know a hash the primary does not. Call counters let a test prove the fallback
    is *not* consulted when the primary answers.
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        seeded: Mapping[str, list[str]] | None = None,
    ) -> None:
        self.fail = fail
        self._seeders: dict[str, list[str]] = {}
        self._manifests: dict[str, ChampionManifest] = {}
        self.announced: list[tuple[ChampionManifest, str]] = []
        self.find_calls = 0
        self.peers_calls = 0
        for content_hash, addrs in (seeded or {}).items():
            self._seeders[content_hash] = list(addrs)
            self._manifests[content_hash] = _manifest(content_hash)

    def announce(self, manifest: ChampionManifest, addr: str) -> None:
        if self.fail:
            raise RuntimeError("transport down")
        self.announced.append((manifest, addr))
        self._manifests[manifest.content_hash] = manifest
        seeders = self._seeders.setdefault(manifest.content_hash, [])
        if addr not in seeders:
            seeders.append(addr)

    def find(self, content_hash: str) -> ArtifactRef | None:
        self.find_calls += 1
        if self.fail:
            raise RuntimeError("transport down")
        seeders = self._seeders.get(content_hash)
        if not seeders:
            return None
        return ArtifactRef(
            content_hash=content_hash,
            seeders=list(seeders),
            manifest=self._manifests[content_hash],
        )

    def peers(self) -> list[str]:
        self.peers_calls += 1
        if self.fail:
            raise RuntimeError("transport down")
        result: list[str] = []
        for seeders in self._seeders.values():
            for addr in seeders:
                if addr not in result:
                    result.append(addr)
        return result


# ─── announce / find / peers on a single transport ───────────────────────


def test_announce_then_find_returns_ref_with_seeder() -> None:
    tracker = FakeTransport()
    d = Discovery(primary=tracker)
    manifest = _manifest("sha256:x")

    d.announce(manifest, "100.64.0.1")
    ref = d.find("sha256:x")

    assert ref is not None
    assert ref.content_hash == "sha256:x"
    assert ref.seeders == ["100.64.0.1"]
    assert ref.manifest == manifest


def test_peers_lists_announced_nodes() -> None:
    d = Discovery(primary=FakeTransport())
    d.announce(_manifest("sha256:a"), "100.64.0.1")
    d.announce(_manifest("sha256:b"), "100.64.0.2")

    assert sorted(d.peers()) == ["100.64.0.1", "100.64.0.2"]


# ─── degrade primary -> fallback on error ────────────────────────────────


def test_find_degrades_to_fallback_on_primary_error() -> None:
    # The plan's canonical case: tracker is down, DHT already knows the hash.
    dht = FakeTransport(seeded={"sha256:x": ["100.x"]})
    d = Discovery(primary=FakeTransport(fail=True), fallback=dht)

    ref = d.find("sha256:x")

    assert ref is not None
    assert ref.seeders == ["100.x"]  # degraded to the DHT


def test_find_degrades_to_fallback_on_primary_empty() -> None:
    # Primary is healthy but has never heard of the hash -> degrade to fallback.
    tracker = FakeTransport()
    dht = FakeTransport(seeded={"sha256:x": ["100.x"]})
    d = Discovery(primary=tracker, fallback=dht)

    ref = d.find("sha256:x")

    assert ref is not None
    assert ref.seeders == ["100.x"]


def test_find_prefers_primary_and_skips_fallback() -> None:
    tracker = FakeTransport(seeded={"sha256:x": ["100.primary"]})
    dht = FakeTransport(seeded={"sha256:x": ["100.fallback"]})
    d = Discovery(primary=tracker, fallback=dht)

    ref = d.find("sha256:x")

    assert ref is not None
    assert ref.seeders == ["100.primary"]
    assert dht.find_calls == 0  # fallback never consulted when primary answers


def test_announce_degrades_to_fallback_on_primary_error() -> None:
    dht = FakeTransport()
    d = Discovery(primary=FakeTransport(fail=True), fallback=dht)
    manifest = _manifest("sha256:x")

    d.announce(manifest, "100.64.0.9")

    assert dht.announced == [(manifest, "100.64.0.9")]


def test_announce_prefers_primary_and_skips_fallback() -> None:
    tracker = FakeTransport()
    dht = FakeTransport()
    d = Discovery(primary=tracker, fallback=dht)

    d.announce(_manifest("sha256:x"), "100.64.0.1")

    assert len(tracker.announced) == 1
    assert dht.announced == []  # fallback untouched on a healthy primary


def test_peers_degrades_to_fallback_on_primary_error() -> None:
    dht = FakeTransport(seeded={"sha256:x": ["100.x"]})
    d = Discovery(primary=FakeTransport(fail=True), fallback=dht)

    assert d.peers() == ["100.x"]


def test_peers_degrades_to_fallback_on_primary_empty() -> None:
    dht = FakeTransport(seeded={"sha256:x": ["100.x"]})
    d = Discovery(primary=FakeTransport(), fallback=dht)

    assert d.peers() == ["100.x"]


# ─── no-fallback behaviour ───────────────────────────────────────────────


def test_find_without_fallback_returns_none_when_absent() -> None:
    d = Discovery(primary=FakeTransport())
    assert d.find("sha256:missing") is None


def test_find_without_fallback_propagates_primary_error() -> None:
    d = Discovery(primary=FakeTransport(fail=True))
    with pytest.raises(RuntimeError):
        d.find("sha256:x")


def test_announce_without_fallback_propagates_primary_error() -> None:
    d = Discovery(primary=FakeTransport(fail=True))
    with pytest.raises(RuntimeError):
        d.announce(_manifest("sha256:x"), "100.64.0.1")
