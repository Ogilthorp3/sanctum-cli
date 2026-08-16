"""MlxLocalProvider — talks to ``sanctum-server`` (or any OpenAI-compatible
local server) over HTTP. No auth, no $$$ cost."""

from __future__ import annotations

import contextlib
import json
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from sanctum_cli.providers.base import (
    Capability,
    ChatOpts,
    HealthSnapshot,
    Message,
    Provider,
    Usage,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sanctum_cli.config import MlxLocalProvider as MlxLocalProviderConfig


HEALTH_TIMEOUT_S = 2.0


class MlxLocalProvider(Provider):
    name = "mlx_local"
    capabilities = Capability.CHAT | Capability.STREAMING

    def __init__(self, cfg: MlxLocalProviderConfig) -> None:
        self._cfg = cfg
        self._client = httpx.Client(base_url=cfg.endpoint, timeout=float(cfg.timeout_s))

    def _body(self, messages: list[Message], opts: ChatOpts) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": opts.stream,
        }
        if opts.max_tokens is not None:
            body["max_tokens"] = opts.max_tokens
        if opts.temperature is not None:
            body["temperature"] = opts.temperature
        return body

    def chat(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
        body = self._body(messages, opts)
        if opts.stream:
            with self._client.stream("POST", "/v1/chat/completions", json=body) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text
            return

        response = self._client.post("/v1/chat/completions", json=body)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            if content:
                yield content

    def health(self) -> HealthSnapshot:
        try:
            t0 = time.perf_counter_ns()
            r = self._client.get("/v1/models", timeout=HEALTH_TIMEOUT_S)
            r.raise_for_status()
            latency_ms = (time.perf_counter_ns() - t0) // 1_000_000
            return HealthSnapshot(
                ok=True, latency_ms=int(latency_ms), quota_remaining=None, detail=None
            )
        except Exception as exc:
            return HealthSnapshot(
                ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160]
            )

    def cost(self, _usage: Usage) -> Decimal:
        return Decimal(0)

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        with contextlib.suppress(Exception):
            self._client.close()
