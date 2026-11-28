"""Sandboxed Apple Silicon compute worker for the Sanctum Hive Compute Grid.

Strict zero-RCE execution contract:
- Verifies every claimed packet's cryptographic signature AND SHA-256 payload hash
  AND zero-RCE allowlist BEFORE touching local Metal silicon.
- Executes ONLY local LLM inference (``sanctum-mlx`` over mTLS or loopback OpenAI
  endpoint) or deterministic eval scoring — NEVER shell commands or arbitrary code.
- Signs every output with the node's post-quantum ``Ed25519 + SLH-DSA-SHA2-128s``
  identity so the coordinator can reconcile Byzantine consensus and award reciprocal
  GPU-second compute credits.
"""

from __future__ import annotations

import os
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from sanctum_cli.errors import LocalError
from sanctum_cli.hive.swarm_crypto import (
    UniversalSwarmSigner,
    build_signed_result,
    verify_packet,
)
from sanctum_cli.hive.swarm_types import SwarmProject, TaskKind, WorkPacket, WorkResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sanctum_cli.mesh.identity import LoadedIdentity, Signer

__all__ = [
    "InferenceBackend",
    "InferenceReply",
    "LocalMlxInferenceBackend",
    "SwarmWorker",
]


@dataclass(frozen=True)
class InferenceReply:
    """Structured output returned by a local Apple Silicon inference backend."""

    text: str
    tokens_generated: int
    elapsed_sec: float
    model: str

    @property
    def tokens_per_sec(self) -> float:
        if self.elapsed_sec <= 0.0:
            return 0.0
        return self.tokens_generated / self.elapsed_sec


class InferenceBackend(Protocol):
    """Injected local inference seam (real ``sanctum-mlx`` or deterministic test fake)."""

    def complete(
        self,
        *,
        prompt: str,
        system: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> InferenceReply: ...


class LocalMlxInferenceBackend:
    """Production local ``sanctum-mlx`` / ``proxyd`` OpenAI-compatible inference client.

    Automatically resolves local mTLS certs under ``~/.sanctum/certs/`` so requests
    to ``https://127.0.0.1:1337`` authenticate natively over TLS 1.3 ``X25519MLKEM768``.
    """

    def __init__(
        self,
        *,
        endpoint: str = "https://127.0.0.1:1337/v1/chat/completions",
        sanctum_dir: Path | None = None,
        timeout: float = 90.0,
    ) -> None:
        self._endpoint = endpoint
        self._sanctum_dir = sanctum_dir or Path(
            os.environ.get("SANCTUM_HOME", str(Path.home() / ".sanctum"))
        )
        self._timeout = timeout

    def _build_ssl_context(self) -> ssl.SSLContext | bool:
        if not self._endpoint.startswith("https://"):
            return True
        certs_dir = self._sanctum_dir / "certs"
        ca = certs_dir / "ca.crt"
        if not ca.is_file():
            return True
        ctx = ssl.create_default_context(cafile=str(ca))
        candidates: list[tuple[Path, Path]] = [
            (certs_dir / "client.crt", certs_dir / "client.key"),
            (certs_dir / "clients" / "canary.crt", certs_dir / "clients" / "canary.key"),
        ]
        clients_dir = certs_dir / "clients"
        if clients_dir.is_dir():
            for crt in sorted(clients_dir.glob("*-cli.crt")):
                candidates.insert(0, (crt, crt.with_suffix(".key")))
        for c_path, k_path in candidates:
            if c_path.is_file() and k_path.is_file():
                ctx.load_cert_chain(certfile=str(c_path), keyfile=str(k_path))
                break
        return ctx

    def complete(
        self,
        *,
        prompt: str,
        system: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> InferenceReply:
        messages: list[dict[str, str]] = []
        if system.strip():
            messages.append({"role": "system", "content": system.strip()})
        messages.append({"role": "user", "content": prompt})

        t0 = time.monotonic()
        try:
            with httpx.Client(verify=self._build_ssl_context(), timeout=self._timeout) as client:
                resp = client.post(
                    self._endpoint,
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            msg = f"local sanctum-mlx inference failed at {self._endpoint}: {exc}"
            raise LocalError(msg, fix="verify `sanctum haus status` or pass --mlx-url") from exc

        elapsed = max(0.001, time.monotonic() - t0)
        choices = body.get("choices") or []
        text = str(((choices[0] or {}).get("message") or {}).get("content") or "") if choices else ""
        usage = body.get("usage") or {}
        toks = int(usage.get("completion_tokens") or max(1, len(text.split())))
        resolved_model = str(body.get("model") or model)
        return InferenceReply(
            text=text,
            tokens_generated=toks,
            elapsed_sec=elapsed,
            model=resolved_model,
        )


class SwarmWorker:
    """Sandboxed Hive Compute Grid worker executing verified inference & eval packets."""

    def __init__(
        self,
        identity: LoadedIdentity,
        backend: InferenceBackend,
        *,
        max_ram_gb: int = 32,
        verifier: Signer | None = None,
    ) -> None:
        self._identity = identity
        self._backend = backend
        self._max_ram_gb = max(4, int(max_ram_gb))
        self._verifier: Signer = verifier or UniversalSwarmSigner(prefer_pq=False)

    def execute_packet(
        self,
        project: SwarmProject,
        packet: WorkPacket,
    ) -> WorkResult:
        """Verify packet trust invariants, execute strictly in-sandbox, and sign the result."""
        if project.required_ram_gb > self._max_ram_gb:
            msg = (
                f"packet requires {project.required_ram_gb} GB RAM, exceeding local worker "
                f"budget ({self._max_ram_gb} GB)"
            )
            raise ValueError(msg)

        ok, reason = verify_packet(packet, verifier=self._verifier)
        if not ok:
            msg = f"refusing untrusted packet {packet.packet_id!r}: {reason}"
            raise ValueError(msg)

        if packet.kind == TaskKind.INFERENCE_MAP:
            return self._run_inference(project, packet)
        if packet.kind == TaskKind.EVAL_BENCHMARK:
            return self._run_eval(project, packet)
        msg = f"worker sandbox does not execute task kind {packet.kind.value!r} directly"
        raise ValueError(msg)

    def _run_inference(self, project: SwarmProject, packet: WorkPacket) -> WorkResult:
        payload: Mapping[str, Any] = packet.payload
        prompt = str(payload["prompt"])
        system = str(payload.get("system") or "You are a precise Sanctum Hive compute node.")
        context = str(payload.get("context") or "")
        full_prompt = f"{context}\n\n{prompt}" if context else prompt
        max_tokens = min(project.max_tokens, int(payload.get("max_tokens") or project.max_tokens))
        temperature = float(payload.get("temperature", 0.0))

        reply = self._backend.complete(
            prompt=full_prompt,
            system=system,
            model=project.base_model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        output: dict[str, Any] = {
            "item_id": str(payload.get("item_id") or packet.packet_id),
            "model": reply.model,
            "response": reply.text,
            "tokens_generated": reply.tokens_generated,
        }
        return build_signed_result(
            self._identity,
            packet_id=packet.packet_id,
            project_id=packet.project_id,
            output=output,
            gpu_seconds=reply.elapsed_sec,
            tokens_per_sec=reply.tokens_per_sec,
            eval_score=None,
        )

    def _run_eval(self, project: SwarmProject, packet: WorkPacket) -> WorkResult:
        payload: Mapping[str, Any] = packet.payload
        prompt = str(payload["prompt"])
        expected = str(payload.get("expected") or "").strip().lower()
        max_tokens = min(project.max_tokens, int(payload.get("max_tokens") or project.max_tokens))
        reply = self._backend.complete(
            prompt=prompt,
            system="Answer concisely and accurately.",
            model=project.base_model,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        answer_norm = reply.text.strip().lower()
        score = 1.0 if (expected and expected in answer_norm) else (0.0 if expected else 1.0)
        output: dict[str, Any] = {
            "case_id": str(payload.get("case_id") or packet.packet_id),
            "model": reply.model,
            "response": reply.text,
            "passed": score >= 0.5,
        }
        return build_signed_result(
            self._identity,
            packet_id=packet.packet_id,
            project_id=packet.project_id,
            output=output,
            gpu_seconds=reply.elapsed_sec,
            tokens_per_sec=reply.tokens_per_sec,
            eval_score=score,
        )
