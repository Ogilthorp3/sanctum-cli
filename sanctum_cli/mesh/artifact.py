"""Content-addressed, signed champion artifacts.

A champion is a directory (a LoRA adapter — ``adapters.safetensors`` +
``adapter_config.json``) or a single file. Before it crosses the mesh it is:

1. **content-addressed** — :func:`content_hash` reduces the bytes to a stable,
   order-independent ``"sha256:…"`` id (a single file's digest, or a
   sorted-file merkle over a directory so filesystem walk order never leaks in);
2. **described + signed** — :func:`build_manifest` fills a
   :class:`~sanctum_cli.mesh.types.ChampionManifest` and signs
   ``content_hash + canonical-manifest-bytes`` with the producer's mesh
   identity;
3. **verified on the far side** — :func:`verify_manifest` recomputes the hash
   (catches a tampered artifact) *and* checks the signature over the same
   canonical bytes (catches tampered metadata or a forged signature). Both gates
   must pass; the hash gate is checked first and short-circuits.

The signing/verifying crypto is an injected seam (the ``Signer`` /
``LoadedIdentity`` from :mod:`sanctum_cli.mesh.identity`); this module is pure
and unit-tested with a deterministic fake. The real ML-DSA signer lives in
``sanctum_cli.mesh.adapters``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from sanctum_cli.mesh.types import ArtifactKind, ChampionManifest

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = [
    "SigningIdentity",
    "VerifyFn",
    "build_manifest",
    "content_hash",
    "verify_manifest",
]

_HASH_PREFIX = "sha256:"
_CHUNK = 1 << 16  # 64 KiB streaming reads — champions are tens of MB.


class SigningIdentity(Protocol):
    """The slice of a mesh identity :func:`build_manifest` needs.

    Structurally satisfied by
    :class:`~sanctum_cli.mesh.identity.LoadedIdentity`: a public key to stamp as
    the producer, and a detached ``sign`` over raw message bytes.
    """

    @property
    def pubkey(self) -> str:
        """The producer's ML-DSA public key (opaque string form)."""
        ...

    def sign(self, message: bytes) -> str:
        """Return a detached signature over ``message``."""
        ...


class VerifyFn(Protocol):
    """The crypto verify seam used by :func:`verify_manifest`.

    Mirrors :meth:`sanctum_cli.mesh.identity.Signer.verify` — argument order is
    ``(public_key, message, signature)`` — so an adapter's bound ``verify`` (or a
    test fake) drops straight in.
    """

    def __call__(self, public_key: str, message: bytes, signature: str) -> bool:
        """Return whether ``signature`` is valid for ``message`` under ``public_key``."""
        ...


def content_hash(path: Path) -> str:
    """Return the stable, order-independent ``"sha256:…"`` id for ``path``.

    A single file hashes its bytes. A directory hashes a sorted-file merkle:
    each contained file contributes ``(relative-posix-path, sha256(bytes))``, the
    entries are sorted by path, and the digest folds them in that fixed order —
    so two directories with the same files hash identically regardless of
    creation order, while a renamed or byte-changed file changes the id.
    """
    if path.is_file():
        return _HASH_PREFIX + _file_digest(path)
    if path.is_dir():
        return _HASH_PREFIX + _dir_digest(path)
    msg = f"artifact path does not exist or is not a file/dir: {path}"
    raise FileNotFoundError(msg)


def build_manifest(
    artifact_path: Path,
    identity: SigningIdentity,
    *,
    base_model: str,
    eval_scores: Mapping[str, float],
    kind: ArtifactKind = ArtifactKind.LORA_ADAPTER,
) -> ChampionManifest:
    """Content-address, describe, and sign ``artifact_path`` as a champion.

    Computes the content hash + on-disk size, fills a
    :class:`~sanctum_cli.mesh.types.ChampionManifest` (producer stamped from
    ``identity.pubkey``), then signs ``content_hash + canonical-manifest-bytes``
    so the returned manifest is self-verifying via :func:`verify_manifest`.
    """
    digest = content_hash(artifact_path)
    unsigned = ChampionManifest(
        content_hash=digest,
        kind=kind,
        base_model=base_model,
        eval_scores=dict(eval_scores),
        size_bytes=_artifact_size(artifact_path),
        producer_pubkey=identity.pubkey,
        signature="",
    )
    signature = identity.sign(_signing_message(unsigned))
    return replace(unsigned, signature=signature)


def verify_manifest(
    artifact_path: Path,
    manifest: ChampionManifest,
    verify_fn: VerifyFn,
) -> bool:
    """Return whether ``manifest`` faithfully describes the artifact on disk.

    Two independent gates, hash first (short-circuits):

    1. the recomputed content hash equals ``manifest.content_hash`` — rejects a
       tampered artifact even if the signature verifier would accept;
    2. ``manifest.signature`` verifies over ``content_hash + canonical bytes``
       under ``manifest.producer_pubkey`` — rejects tampered metadata, a forged
       signature, or a mismatched producer key.
    """
    if content_hash(artifact_path) != manifest.content_hash:
        return False
    return verify_fn(manifest.producer_pubkey, _signing_message(manifest), manifest.signature)


def _signing_message(manifest: ChampionManifest) -> bytes:
    """The exact bytes signed/verified: ``content_hash`` + canonical unsigned form.

    Identical whether the manifest is freshly built (empty signature) or loaded
    with a real one, because the signature field is stripped before canonicalizing.
    """
    return manifest.content_hash.encode("utf-8") + b"\n" + _canonical_bytes(manifest)


def _canonical_bytes(manifest: ChampionManifest) -> bytes:
    """Deterministic JSON of the manifest with the signature field removed.

    ``sort_keys`` + compact separators make the encoding independent of dict
    ordering, so signer and verifier reconstruct byte-identical input.
    """
    payload = manifest.to_dict()
    payload.pop("signature", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _file_digest(path: Path) -> str:
    """Streamed SHA-256 hex of a file's bytes (no prefix)."""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _dir_digest(root: Path) -> str:
    """Sorted-file merkle SHA-256 hex over every file under ``root`` (no prefix)."""
    entries = sorted(
        (p.relative_to(root).as_posix(), _file_digest(p))
        for p in root.rglob("*")
        if p.is_file()
    )
    hasher = hashlib.sha256()
    for rel, digest in entries:
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _artifact_size(path: Path) -> int:
    """Total on-disk byte count: a file's size, or the sum over a directory."""
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
