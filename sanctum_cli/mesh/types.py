"""Core value types for the Sanctum mesh.

These are pure, dependency-free dataclasses and enums shared across the mesh
package (identity, artifact, discovery, adopt-pipeline). Every external
dependency lives behind an injected Protocol elsewhere; this module holds only
data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "ChampionManifest",
    "MeshIdentity",
    "Verdict",
]


class ArtifactKind(StrEnum):
    """What a shared artifact actually is.

    Layer 1 ships LoRA adapters; ``FULL_WEIGHTS`` reserves the seam so Phase 2
    can admit whole-model checkpoints without a schema break.
    """

    LORA_ADAPTER = "lora_adapter"
    FULL_WEIGHTS = "full_weights"


@dataclass(frozen=True)
class MeshIdentity:
    """A haus's public presence on the mesh.

    ``pubkey`` is the ML-DSA public key (opaque string form); ``created`` is an
    ISO-8601 timestamp string. The private key never appears here.
    """

    pubkey: str
    label: str
    created: str


@dataclass(frozen=True)
class ChampionManifest:
    """Signed, content-addressed description of a shareable champion artifact."""

    content_hash: str
    kind: ArtifactKind
    base_model: str
    eval_scores: Mapping[str, float]
    size_bytes: int
    producer_pubkey: str
    signature: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-native dict (enum -> str, scores copied)."""
        return {
            "content_hash": self.content_hash,
            "kind": self.kind.value,
            "base_model": self.base_model,
            "eval_scores": dict(self.eval_scores),
            "size_bytes": self.size_bytes,
            "producer_pubkey": self.producer_pubkey,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChampionManifest:
        """Rebuild a manifest from :meth:`to_dict` output (round-trip exact)."""
        return cls(
            content_hash=data["content_hash"],
            kind=ArtifactKind(data["kind"]),
            base_model=data["base_model"],
            eval_scores=dict(data["eval_scores"]),
            size_bytes=data["size_bytes"],
            producer_pubkey=data["producer_pubkey"],
            signature=data["signature"],
        )


@dataclass(frozen=True)
class ArtifactRef:
    """A discovery hit: a content hash, who is seeding it, and its manifest."""

    content_hash: str
    seeders: list[str]
    manifest: ChampionManifest


@dataclass(frozen=True)
class Verdict:
    """Outcome of the adopt pipeline.

    ``stage`` names the pipeline stage that decided the outcome
    (``hash`` / ``signature`` / ``eval`` / ``sandbox`` / ``promote``);
    ``promoted`` is the single source of truth for whether the local champion
    was replaced.
    """

    promoted: bool
    reason: str
    stage: str
