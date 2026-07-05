"""Unit tests for the content-addressed, signed champion artifact.

Covers the three Task 3 guarantees:

* ``content_hash(path)`` is stable and order-independent (a file's bytes, or a
  sorted-file merkle for a directory — the same set of files hashes the same
  regardless of creation/iteration order).
* ``build_manifest`` produces a manifest whose signature verifies over
  hash + canonical-manifest-bytes.
* ``verify_manifest`` rejects a hash mismatch (tampered artifact) *and* a bad
  signature (tampered manifest / forged sig).

The crypto is the injected ``Signer`` seam from the identity module, driven by a
deterministic non-crypto fake — the real ML-DSA signer lives in adapters.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from sanctum_cli.mesh.artifact import build_manifest, content_hash, verify_manifest
from sanctum_cli.mesh.identity import MeshIdentityStore
from sanctum_cli.mesh.types import ArtifactKind

if TYPE_CHECKING:
    from pathlib import Path


class FakeSigner:
    """Deterministic stand-in for the ML-DSA signer (mirrors the identity fake).

    A keypair shares ``token``; a "signature" is ``sha256(token | message)``.
    ``verify`` recomputes from the *public* token, so a signature verifies iff
    the public key corresponds to the private key that signed it.
    """

    def __init__(self, token: str = "artifact-test-token") -> None:
        self._token = token

    def generate(self) -> tuple[str, str]:
        return (f"fakepub:{self._token}", f"fakepriv:{self._token}")

    def sign(self, private_key: str, message: bytes) -> str:
        token = private_key.split(":", 1)[1]
        return "fakesig:" + hashlib.sha256(token.encode() + b"|" + message).hexdigest()

    def verify(self, public_key: str, message: bytes, signature: str) -> bool:
        token = public_key.split(":", 1)[1]
        expected = "fakesig:" + hashlib.sha256(token.encode() + b"|" + message).hexdigest()
        return signature == expected


def _always_true(public_key: str, message: bytes, signature: str) -> bool:
    """A verifier that accepts anything — proves the hash gate rejects on its own."""
    return True


def _mint(tmp_path: Path) -> tuple[FakeSigner, object]:
    signer = FakeSigner()
    ident = MeshIdentityStore(signer=signer, path=tmp_path / "id").ensure(label="haus-x")
    return signer, ident


# ─── content_hash ────────────────────────────────────────────────────────


def test_content_hash_file_is_prefixed_and_stable(tmp_path: Path) -> None:
    f = tmp_path / "adapter.safetensors"
    f.write_bytes(b"weights-blob")
    first = content_hash(f)
    second = content_hash(f)
    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64  # hex digest


def test_content_hash_file_changes_with_content(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"content-one")
    b.write_bytes(b"content-two")
    assert content_hash(a) != content_hash(b)


def test_content_hash_dir_is_order_independent(tmp_path: Path) -> None:
    # Two directories with the SAME files written in a DIFFERENT order must hash
    # identically — the merkle sorts by relative path, so iteration order is moot.
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "adapter_config.json").write_bytes(b'{"r": 32}')
    (d1 / "adapters.safetensors").write_bytes(b"BINARY-A")
    # d2: same contents, opposite write order + a nested file to exercise recursion.
    (d2 / "adapters.safetensors").write_bytes(b"BINARY-A")
    (d2 / "adapter_config.json").write_bytes(b'{"r": 32}')
    assert content_hash(d1) == content_hash(d2)


def test_content_hash_dir_reflects_content_and_layout(tmp_path: Path) -> None:
    base = tmp_path / "base"
    changed = tmp_path / "changed"
    renamed = tmp_path / "renamed"
    for d in (base, changed, renamed):
        d.mkdir()
    (base / "cfg.json").write_bytes(b'{"r": 32}')
    (base / "weights.bin").write_bytes(b"BLOB")
    # Same names, one byte different -> different hash.
    (changed / "cfg.json").write_bytes(b'{"r": 32}')
    (changed / "weights.bin").write_bytes(b"BLOX")
    # Same bytes, different filename -> different hash (layout is part of identity).
    (renamed / "cfg.json").write_bytes(b'{"r": 32}')
    (renamed / "weights.dat").write_bytes(b"BLOB")
    h_base = content_hash(base)
    assert h_base != content_hash(changed)
    assert h_base != content_hash(renamed)


# ─── build_manifest ──────────────────────────────────────────────────────


def test_build_manifest_fields_and_signature_verifies(tmp_path: Path) -> None:
    artifact = tmp_path / "champion"
    artifact.mkdir()
    (artifact / "adapters.safetensors").write_bytes(b"CHAMPION-WEIGHTS")
    (artifact / "adapter_config.json").write_bytes(b'{"r": 32}')
    signer, ident = _mint(tmp_path)

    manifest = build_manifest(
        artifact,
        ident,  # type: ignore[arg-type]  # LoadedIdentity satisfies SigningIdentity
        base_model="qwen3.6-35b-a3b-4bit",
        eval_scores={"tiered": 0.897},
        kind=ArtifactKind.LORA_ADAPTER,
    )

    assert manifest.content_hash == content_hash(artifact)
    assert manifest.producer_pubkey == "fakepub:artifact-test-token"
    assert manifest.base_model == "qwen3.6-35b-a3b-4bit"
    assert manifest.eval_scores == {"tiered": 0.897}
    assert manifest.kind is ArtifactKind.LORA_ADAPTER
    assert manifest.size_bytes == len(b"CHAMPION-WEIGHTS") + len(b'{"r": 32}')
    assert manifest.signature.startswith("fakesig:")
    # The freshly built signature verifies over the artifact + manifest.
    assert verify_manifest(artifact, manifest, signer.verify) is True


def test_build_manifest_defaults_to_lora_kind(tmp_path: Path) -> None:
    artifact = tmp_path / "adapter.safetensors"
    artifact.write_bytes(b"blob")
    _signer, ident = _mint(tmp_path)
    manifest = build_manifest(
        artifact,
        ident,  # type: ignore[arg-type]
        base_model="qwen",
        eval_scores={"tiered": 0.9},
    )
    assert manifest.kind is ArtifactKind.LORA_ADAPTER


# ─── verify_manifest ─────────────────────────────────────────────────────


def test_verify_manifest_rejects_hash_mismatch(tmp_path: Path) -> None:
    artifact = tmp_path / "adapter.safetensors"
    artifact.write_bytes(b"ORIGINAL")
    signer, ident = _mint(tmp_path)
    manifest = build_manifest(
        artifact,
        ident,  # type: ignore[arg-type]
        base_model="qwen",
        eval_scores={"tiered": 0.9},
    )
    # Tamper the artifact after signing: bytes no longer match the manifest hash.
    artifact.write_bytes(b"TAMPERED")
    # Even an always-accept verifier must not rescue it — the hash gate is first.
    assert verify_manifest(artifact, manifest, _always_true) is False
    assert verify_manifest(artifact, manifest, signer.verify) is False


def test_verify_manifest_rejects_forged_signature(tmp_path: Path) -> None:
    from dataclasses import replace

    artifact = tmp_path / "adapter.safetensors"
    artifact.write_bytes(b"WEIGHTS")
    signer, ident = _mint(tmp_path)
    manifest = build_manifest(
        artifact,
        ident,  # type: ignore[arg-type]
        base_model="qwen",
        eval_scores={"tiered": 0.9},
    )
    forged = replace(manifest, signature="fakesig:deadbeef")
    assert verify_manifest(artifact, forged, signer.verify) is False


def test_verify_manifest_rejects_tampered_metadata(tmp_path: Path) -> None:
    from dataclasses import replace

    artifact = tmp_path / "adapter.safetensors"
    artifact.write_bytes(b"WEIGHTS")
    signer, ident = _mint(tmp_path)
    manifest = build_manifest(
        artifact,
        ident,  # type: ignore[arg-type]
        base_model="qwen",
        eval_scores={"tiered": 0.9},
    )
    # Content unchanged (hash still matches) but a signed field was altered:
    # the signature covers the manifest metadata, so verification must fail.
    tampered = replace(manifest, base_model="evil-backdoored-base")
    assert verify_manifest(artifact, tampered, signer.verify) is False

    forged_scores = replace(manifest, eval_scores={"tiered": 0.999})
    assert verify_manifest(artifact, forged_scores, signer.verify) is False


def test_verify_manifest_rejects_wrong_pubkey(tmp_path: Path) -> None:
    from dataclasses import replace

    artifact = tmp_path / "adapter.safetensors"
    artifact.write_bytes(b"WEIGHTS")
    signer, ident = _mint(tmp_path)
    manifest = build_manifest(
        artifact,
        ident,  # type: ignore[arg-type]
        base_model="qwen",
        eval_scores={"tiered": 0.9},
    )
    # A manifest claiming a different producer key must not verify.
    impostor = replace(manifest, producer_pubkey="fakepub:someone-else")
    assert verify_manifest(artifact, impostor, signer.verify) is False
