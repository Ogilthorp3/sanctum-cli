"""Seed a local champion to the mesh — the mirror of the adopt pipeline.

Where :func:`sanctum_cli.mesh.verify.adopt` decides whether to *accept* a peer's
champion, :func:`seed` decides whether to *offer* one of your own — and it holds
the champion to the SAME bar: it is announced only if it beats the local
baseline on every metric the baseline demands (meets-or-beats, via
:func:`~sanctum_cli.mesh.verify.beats_baseline`). A champion that would regress
the mesh is never advertised.

When the gate passes, seed content-addresses + signs the champion into a
:class:`~sanctum_cli.mesh.types.ChampionManifest` (via
:func:`~sanctum_cli.mesh.artifact.build_manifest`) and announces it — paired
with this node's ``addr`` — through the injected discovery :class:`Announcer`
seam. The real HTTP tracker / DHT transports live in
``sanctum_cli.mesh.adapters``; unit tests drive a recording fake.

The signed manifest that was announced is returned, or ``None`` when the
champion did not clear the baseline (nothing was built or announced).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sanctum_cli.mesh.artifact import build_manifest
from sanctum_cli.mesh.types import ArtifactKind
from sanctum_cli.mesh.verify import beats_baseline

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from sanctum_cli.mesh.artifact import SigningIdentity
    from sanctum_cli.mesh.types import ChampionManifest

__all__ = [
    "Announcer",
    "seed",
]


class Announcer(Protocol):
    """The discovery seam :func:`seed` needs: advertise a champion at an addr.

    Structurally satisfied by both :class:`sanctum_cli.mesh.discovery.Discovery`
    and a raw :class:`~sanctum_cli.mesh.discovery.DiscoveryTransport`, so seed
    stays agnostic to whether a DHT fallback transport is wired in behind it.
    """

    def announce(self, manifest: ChampionManifest, addr: str) -> None:
        """Advertise that ``addr`` seeds the artifact described by ``manifest``."""
        ...


def seed(
    champion_dir: Path,
    *,
    identity: SigningIdentity,
    discovery: Announcer,
    addr: str,
    base_model: str,
    eval_scores: Mapping[str, float],
    baseline_scores: Mapping[str, float],
    kind: ArtifactKind = ArtifactKind.LORA_ADAPTER,
) -> ChampionManifest | None:
    """Sign + announce ``champion_dir`` iff it beats the local baseline.

    Gate first (cheap, and the load-bearing invariant): the champion is
    announced only when ``eval_scores`` meets-or-beats ``baseline_scores`` on
    every metric the baseline names (a missing metric fails the gate — an
    unproven bar is not a cleared one). On a fail nothing is built or announced
    and ``None`` is returned; the mesh never hears about a regressing champion.

    On a pass the champion is content-addressed + signed into a
    :class:`~sanctum_cli.mesh.types.ChampionManifest` (producer stamped from
    ``identity``) and announced — with ``addr`` — via ``discovery``; the signed
    manifest is returned.
    """
    if not _clears_baseline(eval_scores, baseline_scores):
        return None

    manifest = build_manifest(
        champion_dir,
        identity,
        base_model=base_model,
        eval_scores=eval_scores,
        kind=kind,
    )
    discovery.announce(manifest, addr)
    return manifest


def _clears_baseline(
    eval_scores: Mapping[str, float],
    baseline_scores: Mapping[str, float],
) -> bool:
    """Return whether ``eval_scores`` meets-or-beats every baseline metric.

    Reuses :func:`~sanctum_cli.mesh.verify.beats_baseline` per metric so seeding
    and adopting apply one consistent regression bar. A baseline metric absent
    from ``eval_scores`` fails the gate: a champion cannot prove it clears a bar
    it never measured. An empty baseline is vacuously cleared (no bar set).
    """
    return all(
        metric in eval_scores and beats_baseline(eval_scores[metric], required)
        for metric, required in baseline_scores.items()
    )
