"""Post-quantum composite signing and domain-separated verification for Hive Compute Grid.

Uses ``CompositeSlhDsaEd25519Signer`` (Ed25519 + NIST FIPS 205 SLH-DSA-SHA2-128s)
whenever OpenSSL 3.5+/4.x is present, and seamlessly falls back to Ed25519 when
running on minimal hosts without FIPS 205 OpenSSL. Verification automatically
dispatches on the key prefix so mixed fleets interoperate cleanly without losing
post-quantum strength on upgraded Macs.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sanctum_cli.hive.swarm_types import (
    ConsensusMode,
    DiLoCoDelta,
    SwarmProject,
    TaskKind,
    TrustZone,
    WorkPacket,
    WorkResult,
    canonical_json_bytes,
    sha256_canonical,
    validate_safe_payload,
)
from sanctum_cli.mesh.adapters import (
    CompositeSlhDsaEd25519Signer,
    Ed25519Signer,
    slh_dsa_available,
)
from sanctum_cli.mesh.identity import LoadedIdentity, MeshIdentityStore, Signer

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "UniversalSwarmSigner",
    "build_signed_diloco_delta",
    "build_signed_packet",
    "build_signed_project",
    "build_signed_result",
    "default_swarm_identity_dir",
    "ensure_swarm_identity",
    "pubkey_fingerprint",
    "verify_diloco_delta",
    "verify_packet",
    "verify_project",
    "verify_result",
]

_DOMAIN_PROJECT = b"sanctum-swarm-project-v1:"
_DOMAIN_PACKET = b"sanctum-swarm-packet-v1:"
_DOMAIN_RESULT = b"sanctum-swarm-result-v1:"
_DOMAIN_DILOCO = b"sanctum-swarm-diloco-v1:"
_SLH_PREFIX = "ed25519+slh-dsa-sha2-128s:"


class UniversalSwarmSigner:
    """Hybrid PQ/Ed25519 :class:`Signer` that verifies both key schemes transparently."""

    def __init__(self, *, prefer_pq: bool = True, openssl_bin: str | None = None) -> None:
        self._ed = Ed25519Signer()
        self._prefer_pq = prefer_pq
        self._pq: CompositeSlhDsaEd25519Signer | None = None
        if prefer_pq and (openssl_bin is not None or slh_dsa_available()):
            self._pq = CompositeSlhDsaEd25519Signer(openssl_bin=openssl_bin)

    @property
    def is_post_quantum(self) -> bool:
        return self._pq is not None

    def generate(self) -> tuple[str, str]:
        if self._pq is not None:
            return self._pq.generate()
        return self._ed.generate()

    def sign(self, private_key: str, message: bytes) -> str:
        if private_key.startswith(_SLH_PREFIX):
            pq = self._pq or CompositeSlhDsaEd25519Signer()
            return pq.sign(private_key, message)
        return self._ed.sign(private_key, message)

    def verify(self, public_key: str, message: bytes, signature: str) -> bool:
        if public_key.startswith(_SLH_PREFIX) or signature.startswith(_SLH_PREFIX):
            if not (public_key.startswith(_SLH_PREFIX) and signature.startswith(_SLH_PREFIX)):
                return False
            pq = self._pq or CompositeSlhDsaEd25519Signer()
            return pq.verify(public_key, message, signature)
        return self._ed.verify(public_key, message, signature)


def default_swarm_identity_dir() -> Path:
    root = Path(os.environ.get("SANCTUM_HOME", str(Path.home() / ".sanctum")))
    return root / "hive" / "swarm-identity"


def ensure_swarm_identity(
    label: str = "sanctum-node",
    *,
    identity_dir: Path | None = None,
    prefer_pq: bool = True,
    signer: Signer | None = None,
) -> LoadedIdentity:
    """Mint (mode 0600) or load this node's cryptographic Hive Compute Grid identity."""
    active_signer: Signer = signer or UniversalSwarmSigner(prefer_pq=prefer_pq)
    store = MeshIdentityStore(signer=active_signer, path=identity_dir or default_swarm_identity_dir())
    return store.ensure(label)


def pubkey_fingerprint(pubkey: str) -> str:
    """Return a short 16-char uppercase SHA-256 fingerprint for display/logging."""
    return hashlib.sha256(pubkey.encode("utf-8")).hexdigest()[:16].upper()


def _project_bytes(project: SwarmProject) -> bytes:
    return _DOMAIN_PROJECT + canonical_json_bytes(project.signable_dict())


def _packet_bytes(packet: WorkPacket) -> bytes:
    return _DOMAIN_PACKET + canonical_json_bytes(packet.signable_dict())


def _result_bytes(result: WorkResult) -> bytes:
    return _DOMAIN_RESULT + canonical_json_bytes(result.signable_dict())


def _diloco_bytes(delta: DiLoCoDelta) -> bytes:
    return _DOMAIN_DILOCO + canonical_json_bytes(delta.signable_dict())


def build_signed_project(
    identity: LoadedIdentity,
    *,
    title: str,
    kind: TaskKind,
    description: str = "",
    trust_zone: TrustZone = TrustZone.OPEN_HIVE,
    base_model: str = "Qwen3.8-27B-4bit",
    required_ram_gb: int = 16,
    redundancy: int = 1,
    consensus_mode: ConsensusMode = ConsensusMode.EXACT_HASH,
    max_tokens: int = 512,
    project_id: str | None = None,
) -> SwarmProject:
    """Construct and cryptographically sign a :class:`SwarmProject`."""
    if kind == TaskKind.PIPELINE_STAGE and trust_zone != TrustZone.TRUSTED_CIRCLE:
        msg = "pipeline_stage workloads are restricted strictly to trusted_circle"
        raise ValueError(msg)
    if redundancy < 1 or redundancy > 5:
        msg = "redundancy must be between 1 and 5"
        raise ValueError(msg)
    pid = project_id or f"proj-{secrets.token_hex(6)}"
    created_at = datetime.now(UTC).isoformat()
    unsigned = SwarmProject(
        project_id=pid,
        title=title.strip(),
        description=description.strip(),
        kind=kind,
        trust_zone=trust_zone,
        base_model=base_model.strip(),
        required_ram_gb=max(4, int(required_ram_gb)),
        redundancy=redundancy,
        consensus_mode=consensus_mode,
        max_tokens=max(16, int(max_tokens)),
        creator_pubkey=identity.pubkey,
        created_at=created_at,
        signature="",
    )
    sig = identity.sign(_project_bytes(unsigned))
    return SwarmProject(**{**unsigned.signable_dict(), "kind": kind, "trust_zone": trust_zone, "consensus_mode": consensus_mode, "signature": sig})


def verify_project(project: SwarmProject, *, verifier: Signer | None = None) -> bool:
    """Return True iff the project's policy invariants AND cryptographic signature hold."""
    if project.kind == TaskKind.PIPELINE_STAGE and project.trust_zone != TrustZone.TRUSTED_CIRCLE:
        return False
    if project.redundancy < 1 or project.redundancy > 5:
        return False
    if not project.title.strip() or not project.project_id.strip():
        return False
    sig_verifier = verifier or UniversalSwarmSigner(prefer_pq=False)
    return sig_verifier.verify(project.creator_pubkey, _project_bytes(project), project.signature)


def build_signed_packet(
    identity: LoadedIdentity,
    *,
    project_id: str,
    seq: int,
    kind: TaskKind,
    payload: Mapping[str, Any],
    packet_id: str | None = None,
) -> WorkPacket:
    """Validate payload zero-RCE safety, hash it, and sign a :class:`WorkPacket`."""
    validate_safe_payload(kind, payload)
    payload_copy = dict(payload)
    payload_hash = sha256_canonical(payload_copy)
    pid = packet_id or f"{project_id}-p{seq:04d}-{secrets.token_hex(4)}"
    unsigned = WorkPacket(
        packet_id=pid,
        project_id=project_id,
        seq=seq,
        kind=kind,
        payload_hash=payload_hash,
        payload=payload_copy,
        creator_pubkey=identity.pubkey,
        signature="",
    )
    sig = identity.sign(_packet_bytes(unsigned))
    return WorkPacket(
        packet_id=pid,
        project_id=project_id,
        seq=seq,
        kind=kind,
        payload_hash=payload_hash,
        payload=payload_copy,
        creator_pubkey=identity.pubkey,
        signature=sig,
    )


def verify_packet(packet: WorkPacket, *, verifier: Signer | None = None) -> tuple[bool, str]:
    """Verify zero-RCE payload safety, content hash integrity, and creator signature."""
    try:
        validate_safe_payload(packet.kind, packet.payload)
    except ValueError as exc:
        return (False, f"unsafe_payload: {exc}")
    expected_hash = sha256_canonical(dict(packet.payload))
    if expected_hash != packet.payload_hash:
        return (False, "payload_hash_mismatch")
    sig_verifier = verifier or UniversalSwarmSigner(prefer_pq=False)
    if not sig_verifier.verify(packet.creator_pubkey, _packet_bytes(packet), packet.signature):
        return (False, "invalid_signature")
    return (True, "ok")


def build_signed_result(
    identity: LoadedIdentity,
    *,
    packet_id: str,
    project_id: str,
    output: Mapping[str, Any],
    gpu_seconds: float,
    tokens_per_sec: float = 0.0,
    eval_score: float | None = None,
    result_id: str | None = None,
) -> WorkResult:
    """Hash output, enforce output size cap, and sign a :class:`WorkResult`."""
    output_copy = dict(output)
    output_hash = sha256_canonical(output_copy)
    rid = result_id or f"res-{secrets.token_hex(6)}"
    submitted_at = datetime.now(UTC).isoformat()
    unsigned = WorkResult(
        result_id=rid,
        packet_id=packet_id,
        project_id=project_id,
        worker_pubkey=identity.pubkey,
        output_hash=output_hash,
        output=output_copy,
        gpu_seconds=max(0.0, float(gpu_seconds)),
        tokens_per_sec=max(0.0, float(tokens_per_sec)),
        eval_score=eval_score,
        submitted_at=submitted_at,
        signature="",
    )
    sig = identity.sign(_result_bytes(unsigned))
    return WorkResult(
        result_id=rid,
        packet_id=packet_id,
        project_id=project_id,
        worker_pubkey=identity.pubkey,
        output_hash=output_hash,
        output=output_copy,
        gpu_seconds=unsigned.gpu_seconds,
        tokens_per_sec=unsigned.tokens_per_sec,
        eval_score=eval_score,
        submitted_at=submitted_at,
        signature=sig,
    )


def verify_result(result: WorkResult, *, verifier: Signer | None = None) -> tuple[bool, str]:
    """Verify output hash integrity, size limit, and worker signature."""
    from sanctum_cli.hive.swarm_types import MAX_OUTPUT_BYTES

    raw = canonical_json_bytes(dict(result.output))
    if len(raw) > MAX_OUTPUT_BYTES:
        return (False, "output_too_large")
    if sha256_canonical(dict(result.output)) != result.output_hash:
        return (False, "output_hash_mismatch")
    sig_verifier = verifier or UniversalSwarmSigner(prefer_pq=False)
    if not sig_verifier.verify(result.worker_pubkey, _result_bytes(result), result.signature):
        return (False, "invalid_signature")
    return (True, "ok")


def build_signed_diloco_delta(
    identity: LoadedIdentity,
    *,
    project_id: str,
    round_num: int,
    base_hash: str,
    delta_hash: str,
    local_steps: int,
    num_samples: int,
    eval_loss_before: float,
    eval_loss_after: float,
    delta_l2_norm: float,
    size_bytes: int,
    gpu_seconds: float,
) -> DiLoCoDelta:
    """Construct and sign a federated LoRA DiLoCo outer-update submission."""
    submitted_at = datetime.now(UTC).isoformat()
    unsigned = DiLoCoDelta(
        project_id=project_id,
        round_num=round_num,
        worker_pubkey=identity.pubkey,
        base_hash=base_hash,
        delta_hash=delta_hash,
        local_steps=local_steps,
        num_samples=num_samples,
        eval_loss_before=eval_loss_before,
        eval_loss_after=eval_loss_after,
        delta_l2_norm=delta_l2_norm,
        size_bytes=size_bytes,
        gpu_seconds=max(0.0, float(gpu_seconds)),
        submitted_at=submitted_at,
        signature="",
    )
    sig = identity.sign(_diloco_bytes(unsigned))
    return DiLoCoDelta(
        project_id=project_id,
        round_num=round_num,
        worker_pubkey=identity.pubkey,
        base_hash=base_hash,
        delta_hash=delta_hash,
        local_steps=local_steps,
        num_samples=num_samples,
        eval_loss_before=eval_loss_before,
        eval_loss_after=eval_loss_after,
        delta_l2_norm=delta_l2_norm,
        size_bytes=size_bytes,
        gpu_seconds=unsigned.gpu_seconds,
        submitted_at=submitted_at,
        signature=sig,
    )


def verify_diloco_delta(delta: DiLoCoDelta, *, verifier: Signer | None = None) -> tuple[bool, str]:
    """Verify basic bounds and cryptographic signature of a :class:`DiLoCoDelta`."""
    if delta.local_steps <= 0 or delta.num_samples <= 0 or delta.size_bytes <= 0:
        return (False, "invalid_training_bounds")
    if delta.delta_l2_norm <= 0.0:
        return (False, "non_positive_l2_norm")
    sig_verifier = verifier or UniversalSwarmSigner(prefer_pq=False)
    if not sig_verifier.verify(delta.worker_pubkey, _diloco_bytes(delta), delta.signature):
        return (False, "invalid_signature")
    return (True, "ok")
