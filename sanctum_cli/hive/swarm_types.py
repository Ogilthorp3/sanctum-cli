"""Pure value types and zero-RCE payload contracts for the Sanctum Hive Compute Grid.

Every type here is frozen, dependency-free, and JSON-canonical so work manifests,
packets, signed results, DiLoCo federated LoRA deltas, and reciprocal compute
ledger entries round-trip deterministically across Apple Silicon nodes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "MAX_OUTPUT_BYTES",
    "MAX_PAYLOAD_BYTES",
    "ConsensusMode",
    "CreditAccount",
    "DiLoCoDelta",
    "DiLoCoRoundSummary",
    "PacketStatus",
    "PriorityTier",
    "SwarmProject",
    "TaskKind",
    "TrustZone",
    "WorkPacket",
    "WorkResult",
    "WorkerNode",
    "canonical_json_bytes",
    "sha256_canonical",
    "validate_safe_payload",
]

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 128 * 1024

_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "bash",
        "binary",
        "cmd",
        "code",
        "command",
        "env",
        "eval",
        "exec",
        "executable",
        "file_path",
        "filepath",
        "path",
        "python",
        "script",
        "sh",
        "shell",
        "subprocess",
    }
)


class TaskKind(StrEnum):
    """Sandboxed compute workloads admitted onto the Sanctum Hive Compute Grid."""

    INFERENCE_MAP = "inference_map"
    EVAL_BENCHMARK = "eval_benchmark"
    DILOCO_STEP = "diloco_step"
    PIPELINE_STAGE = "pipeline_stage"


class TrustZone(StrEnum):
    """Isolation boundary controlling which nodes may participate in a project."""

    OPEN_HIVE = "open_hive"
    TRUSTED_CIRCLE = "trusted_circle"


class PacketStatus(StrEnum):
    """Lifecycle states of a verifiable compute work packet."""

    PENDING = "pending"
    LEASED = "leased"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    REJECTED = "rejected"


class ConsensusMode(StrEnum):
    """How redundant worker submissions are reconciled for Byzantine fault tolerance."""

    EXACT_HASH = "exact_hash"
    SCORE_TOLERANCE = "score_tolerance"


class PriorityTier(StrEnum):
    """Reciprocal BitTorrent-style compute scheduling priority (zero tokens/coins)."""

    VANGUARD = "vanguard"
    CONTRIBUTOR = "contributor"
    SEED = "seed"
    PROBATION = "probation"


_ALLOWED_PAYLOAD_KEYS: dict[TaskKind, frozenset[str]] = {
    TaskKind.INFERENCE_MAP: frozenset(
        {"item_id", "prompt", "system", "context", "max_tokens", "temperature"}
    ),
    TaskKind.EVAL_BENCHMARK: frozenset(
        {"case_id", "prompt", "expected", "rubric", "max_tokens", "temperature"}
    ),
    TaskKind.DILOCO_STEP: frozenset(
        {
            "round_num",
            "base_model",
            "base_hash",
            "dataset_ref",
            "local_steps",
            "learning_rate",
            "lora_rank",
        }
    ),
    TaskKind.PIPELINE_STAGE: frozenset(
        {"request_id", "layer_start", "layer_end", "hidden_hash", "seq_len"}
    ),
}


def canonical_json_bytes(obj: Any) -> bytes:
    """Serialize any JSON-native value into deterministic UTF-8 canonical bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def sha256_canonical(obj: Any) -> str:
    """Return ``sha256:<64-hex>`` over the canonical JSON encoding of ``obj``."""
    return f"sha256:{hashlib.sha256(canonical_json_bytes(obj)).hexdigest()}"


def _walk_check_safe(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        msg = "payload nesting depth exceeds safety bound (8)"
        raise ValueError(msg)
    if isinstance(value, str):
        if "\x00" in value:
            msg = "null byte forbidden in swarm payload"
            raise ValueError(msg)
        if "../" in value or "/.." in value or value == "..":
            msg = "path traversal sequence forbidden in swarm payload"
            raise ValueError(msg)
    elif isinstance(value, Mapping):
        for raw_k, v in value.items():
            key = str(raw_k).strip().lower()
            if key in _FORBIDDEN_KEYS:
                msg = f"forbidden execution key {raw_k!r} rejected by zero-RCE guard"
                raise ValueError(msg)
            _walk_check_safe(v, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 512:
            msg = "payload list length exceeds safety bound (512)"
            raise ValueError(msg)
        for item in value:
            _walk_check_safe(item, depth=depth + 1)
    elif value is None or isinstance(value, (bool, int, float)):
        return
    else:
        msg = f"unsupported payload value type {type(value).__name__}"
        raise ValueError(msg)


def validate_safe_payload(kind: TaskKind, payload: Mapping[str, Any]) -> None:
    """Enforce strict allowlist + zero-RCE + size cap validation on a packet payload."""
    if not isinstance(payload, Mapping):
        msg = "payload must be a JSON object"
        raise ValueError(msg)
    raw = canonical_json_bytes(dict(payload))
    if len(raw) > MAX_PAYLOAD_BYTES:
        msg = f"payload size {len(raw)} exceeds {MAX_PAYLOAD_BYTES} byte limit"
        raise ValueError(msg)

    allowed = _ALLOWED_PAYLOAD_KEYS[kind]
    for raw_k in payload:
        key = str(raw_k).strip()
        if key.lower() in _FORBIDDEN_KEYS:
            msg = f"forbidden execution key {raw_k!r} rejected by zero-RCE guard"
            raise ValueError(msg)
        if key not in allowed:
            msg = f"unexpected payload key {raw_k!r} for task kind {kind.value}"
            raise ValueError(msg)

    _walk_check_safe(payload)

    if kind == TaskKind.INFERENCE_MAP and not str(payload.get("prompt", "")).strip():
        msg = "inference_map payload requires non-empty 'prompt'"
        raise ValueError(msg)
    if kind == TaskKind.EVAL_BENCHMARK and not str(payload.get("prompt", "")).strip():
        msg = "eval_benchmark payload requires non-empty 'prompt'"
        raise ValueError(msg)
    if kind == TaskKind.DILOCO_STEP:
        steps = int(payload.get("local_steps", 0))
        if steps <= 0 or steps > 10_000:
            msg = "diloco_step requires 1 <= local_steps <= 10000"
            raise ValueError(msg)


@dataclass(frozen=True)
class WorkerNode:
    """A registered Apple Silicon Mac contributing capacity to the Hive Compute Grid."""

    node_id: str
    pubkey: str
    chip: str
    memory_gb: int
    trust_zone: TrustZone
    supported_kinds: tuple[TaskKind, ...]
    last_seen_epoch: float
    psk_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "pubkey": self.pubkey,
            "chip": self.chip,
            "memory_gb": self.memory_gb,
            "trust_zone": self.trust_zone.value,
            "supported_kinds": [k.value for k in self.supported_kinds],
            "last_seen_epoch": self.last_seen_epoch,
            "psk_fingerprint": self.psk_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkerNode:
        raw_kinds = data.get("supported_kinds") or [TaskKind.INFERENCE_MAP.value]
        return cls(
            node_id=str(data["node_id"]),
            pubkey=str(data["pubkey"]),
            chip=str(data.get("chip", "Apple Silicon")),
            memory_gb=int(data.get("memory_gb", 0)),
            trust_zone=TrustZone(str(data.get("trust_zone", TrustZone.OPEN_HIVE.value))),
            supported_kinds=tuple(TaskKind(str(k)) for k in raw_kinds),
            last_seen_epoch=float(data.get("last_seen_epoch", 0.0)),
            psk_fingerprint=str(data.get("psk_fingerprint", "")),
        )


@dataclass(frozen=True)
class SwarmProject:
    """Signed project manifest distributed across participating Hive Macs."""

    project_id: str
    title: str
    description: str
    kind: TaskKind
    trust_zone: TrustZone
    base_model: str
    required_ram_gb: int
    redundancy: int
    consensus_mode: ConsensusMode
    max_tokens: int
    creator_pubkey: str
    created_at: str
    signature: str

    def signable_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "description": self.description,
            "kind": self.kind.value,
            "trust_zone": self.trust_zone.value,
            "base_model": self.base_model,
            "required_ram_gb": self.required_ram_gb,
            "redundancy": self.redundancy,
            "consensus_mode": self.consensus_mode.value,
            "max_tokens": self.max_tokens,
            "creator_pubkey": self.creator_pubkey,
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.signable_dict(), "signature": self.signature}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SwarmProject:
        return cls(
            project_id=str(data["project_id"]),
            title=str(data["title"]),
            description=str(data.get("description", "")),
            kind=TaskKind(str(data["kind"])),
            trust_zone=TrustZone(str(data.get("trust_zone", TrustZone.OPEN_HIVE.value))),
            base_model=str(data.get("base_model", "Qwen3.8-27B-4bit")),
            required_ram_gb=int(data.get("required_ram_gb", 16)),
            redundancy=int(data.get("redundancy", 1)),
            consensus_mode=ConsensusMode(
                str(data.get("consensus_mode", ConsensusMode.EXACT_HASH.value))
            ),
            max_tokens=int(data.get("max_tokens", 512)),
            creator_pubkey=str(data["creator_pubkey"]),
            created_at=str(data["created_at"]),
            signature=str(data["signature"]),
        )


@dataclass(frozen=True)
class WorkPacket:
    """A single signed, content-addressed, zero-RCE compute unit."""

    packet_id: str
    project_id: str
    seq: int
    kind: TaskKind
    payload_hash: str
    payload: Mapping[str, Any]
    creator_pubkey: str
    signature: str
    status: PacketStatus = PacketStatus.PENDING
    assigned_workers: tuple[str, ...] = ()

    def signable_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "project_id": self.project_id,
            "seq": self.seq,
            "kind": self.kind.value,
            "payload_hash": self.payload_hash,
            "creator_pubkey": self.creator_pubkey,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.signable_dict(),
            "payload": dict(self.payload),
            "signature": self.signature,
            "status": self.status.value,
            "assigned_workers": list(self.assigned_workers),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkPacket:
        return cls(
            packet_id=str(data["packet_id"]),
            project_id=str(data["project_id"]),
            seq=int(data["seq"]),
            kind=TaskKind(str(data["kind"])),
            payload_hash=str(data["payload_hash"]),
            payload=dict(data["payload"]),
            creator_pubkey=str(data["creator_pubkey"]),
            signature=str(data["signature"]),
            status=PacketStatus(str(data.get("status", PacketStatus.PENDING.value))),
            assigned_workers=tuple(str(w) for w in (data.get("assigned_workers") or ())),
        )


@dataclass(frozen=True)
class WorkResult:
    """Cryptographically signed execution output returned by a sandboxed worker."""

    result_id: str
    packet_id: str
    project_id: str
    worker_pubkey: str
    output_hash: str
    output: Mapping[str, Any]
    gpu_seconds: float
    tokens_per_sec: float
    eval_score: float | None
    submitted_at: str
    signature: str

    def signable_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "packet_id": self.packet_id,
            "project_id": self.project_id,
            "worker_pubkey": self.worker_pubkey,
            "output_hash": self.output_hash,
            "gpu_seconds": round(float(self.gpu_seconds), 4),
            "tokens_per_sec": round(float(self.tokens_per_sec), 3),
            "eval_score": (
                round(float(self.eval_score), 6) if self.eval_score is not None else None
            ),
            "submitted_at": self.submitted_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.signable_dict(),
            "output": dict(self.output),
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkResult:
        raw_score = data.get("eval_score")
        return cls(
            result_id=str(data["result_id"]),
            packet_id=str(data["packet_id"]),
            project_id=str(data["project_id"]),
            worker_pubkey=str(data["worker_pubkey"]),
            output_hash=str(data["output_hash"]),
            output=dict(data["output"]),
            gpu_seconds=float(data.get("gpu_seconds", 0.0)),
            tokens_per_sec=float(data.get("tokens_per_sec", 0.0)),
            eval_score=float(raw_score) if raw_score is not None else None,
            submitted_at=str(data["submitted_at"]),
            signature=str(data["signature"]),
        )


@dataclass(frozen=True)
class DiLoCoDelta:
    """Signed low-communication federated LoRA outer-update from one Mac."""

    project_id: str
    round_num: int
    worker_pubkey: str
    base_hash: str
    delta_hash: str
    local_steps: int
    num_samples: int
    eval_loss_before: float
    eval_loss_after: float
    delta_l2_norm: float
    size_bytes: int
    gpu_seconds: float
    submitted_at: str
    signature: str

    def signable_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "round_num": self.round_num,
            "worker_pubkey": self.worker_pubkey,
            "base_hash": self.base_hash,
            "delta_hash": self.delta_hash,
            "local_steps": self.local_steps,
            "num_samples": self.num_samples,
            "eval_loss_before": round(float(self.eval_loss_before), 6),
            "eval_loss_after": round(float(self.eval_loss_after), 6),
            "delta_l2_norm": round(float(self.delta_l2_norm), 6),
            "size_bytes": self.size_bytes,
            "gpu_seconds": round(float(self.gpu_seconds), 4),
            "submitted_at": self.submitted_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.signable_dict(), "signature": self.signature}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DiLoCoDelta:
        return cls(
            project_id=str(data["project_id"]),
            round_num=int(data["round_num"]),
            worker_pubkey=str(data["worker_pubkey"]),
            base_hash=str(data["base_hash"]),
            delta_hash=str(data["delta_hash"]),
            local_steps=int(data["local_steps"]),
            num_samples=int(data["num_samples"]),
            eval_loss_before=float(data["eval_loss_before"]),
            eval_loss_after=float(data["eval_loss_after"]),
            delta_l2_norm=float(data["delta_l2_norm"]),
            size_bytes=int(data["size_bytes"]),
            gpu_seconds=float(data.get("gpu_seconds", 0.0)),
            submitted_at=str(data["submitted_at"]),
            signature=str(data["signature"]),
        )


@dataclass(frozen=True)
class DiLoCoRoundSummary:
    """Auditable Byzantine-filtered aggregation plan for one DiLoCo training round."""

    project_id: str
    round_num: int
    accepted_workers: tuple[str, ...]
    rejected_workers: Mapping[str, str]
    mixing_weights: Mapping[str, float]
    total_samples: int
    mean_loss_improvement: float
    round_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "round_num": self.round_num,
            "accepted_workers": list(self.accepted_workers),
            "rejected_workers": dict(self.rejected_workers),
            "mixing_weights": dict(self.mixing_weights),
            "total_samples": self.total_samples,
            "mean_loss_improvement": round(self.mean_loss_improvement, 6),
            "round_hash": self.round_hash,
        }


@dataclass(frozen=True)
class CreditAccount:
    """Reciprocal GPU-second accounting for one node (zero currency/tokenomics)."""

    pubkey: str
    node_id: str
    contributed_gpu_sec: float = 0.0
    consumed_gpu_sec: float = 0.0
    verified_packets: int = 0
    rejected_packets: int = 0
    diloco_deltas_accepted: int = 0
    reputation: float = 1.0
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def net_gpu_sec(self) -> float:
        return round(self.contributed_gpu_sec - self.consumed_gpu_sec, 4)

    @property
    def priority_tier(self) -> PriorityTier:
        if self.reputation < 0.4:
            return PriorityTier.PROBATION
        if self.net_gpu_sec >= 60.0 and self.verified_packets >= 5 and self.reputation >= 0.85:
            return PriorityTier.VANGUARD
        if self.contributed_gpu_sec > 0.0 and self.verified_packets >= 1:
            return PriorityTier.CONTRIBUTOR
        return PriorityTier.SEED

    def to_dict(self) -> dict[str, Any]:
        return {
            "pubkey": self.pubkey,
            "node_id": self.node_id,
            "contributed_gpu_sec": round(self.contributed_gpu_sec, 4),
            "consumed_gpu_sec": round(self.consumed_gpu_sec, 4),
            "net_gpu_sec": self.net_gpu_sec,
            "verified_packets": self.verified_packets,
            "rejected_packets": self.rejected_packets,
            "diloco_deltas_accepted": self.diloco_deltas_accepted,
            "reputation": round(self.reputation, 4),
            "priority_tier": self.priority_tier.value,
        }
