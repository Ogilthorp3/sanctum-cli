"""ClaudeProvider — two flavours, one interface.

``via=proxy`` (default) talks to a local OpenAI-compatible proxy that
shells out to the ``claude`` CLI; billing flows through the user's Max
subscription with zero per-token API charges.

``via=direct`` uses the official Anthropic SDK against api.anthropic.com
and bills the API key in Keychain.

The interface (the ``Provider`` ABC) doesn't care which mode is active —
``chat()`` yields text chunks, ``health()`` returns a HealthSnapshot,
``cost()`` returns USD. ``via=proxy`` reports cost as zero because the
Max subscription is flat-rate.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

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

    from sanctum_cli.config import ClaudeProvider as ClaudeProviderConfig


# Approximate Opus 4.x pricing per 1M tokens (USD). Override per-config in v0.7.
INPUT_USD_PER_M = Decimal("15.00")
OUTPUT_USD_PER_M = Decimal("75.00")
HEALTH_TIMEOUT_S = 5.0


class ClaudeProvider(Provider):
    name = "claude"
    capabilities = Capability.CHAT | Capability.TOOLS | Capability.STREAMING | Capability.THINKING

    def __init__(self, cfg: ClaudeProviderConfig, api_key: str | None) -> None:
        self._cfg = cfg
        self._api_key = api_key
        if cfg.via == "direct":
            from anthropic import Anthropic

            if not api_key:
                msg = "via=direct requires an API key from Keychain"
                raise ValueError(msg)
            self._sdk: Any = Anthropic(
                api_key=api_key,
                base_url=cfg.endpoint,
                timeout=float(cfg.timeout_s),
                max_retries=cfg.max_retries,
            )
        else:
            self._sdk = None
        self._http = httpx.Client(base_url=cfg.endpoint, timeout=float(cfg.timeout_s))

    # ─── chat ──────────────────────────────────────────────────────

    def chat(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
        if self._cfg.via == "direct":
            yield from self._chat_direct(messages, opts)
        else:
            yield from self._chat_proxy(messages, opts)

    # Direct API path — full anthropic SDK with streaming
    def _split_system(self, messages: list[Message]) -> tuple[str, list[Message]]:
        system_parts = [m.content for m in messages if m.role == "system"]
        chat_msgs = [m for m in messages if m.role != "system"]
        return "\n\n".join(system_parts), chat_msgs

    def _chat_direct(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
        system, chat_msgs = self._split_system(messages)
        anth_msgs = cast("Any", [{"role": m.role, "content": m.content} for m in chat_msgs])
        max_tokens = opts.max_tokens or self._cfg.max_tokens
        extra: dict[str, Any] = {}
        if system:
            extra["system"] = system
        if opts.temperature is not None:
            extra["temperature"] = opts.temperature

        if opts.stream:
            with self._sdk.messages.stream(
                model=self._cfg.model,
                messages=anth_msgs,
                max_tokens=max_tokens,
                **extra,
            ) as stream:
                yield from stream.text_stream
            return

        response = self._sdk.messages.create(
            model=self._cfg.model,
            messages=anth_msgs,
            max_tokens=max_tokens,
            **extra,
        )
        for block in response.content:
            if block.type == "text":
                yield block.text

    # Proxy path — OpenAI-compat HTTP, claude-cli-proxy on :2001
    def _chat_proxy(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
        body: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,  # claude-cli-proxy does not stream — yield once
            "max_tokens": opts.max_tokens or self._cfg.max_tokens,
        }
        if opts.temperature is not None:
            body["temperature"] = opts.temperature
        response = self._http.post("/v1/chat/completions", json=body)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return
        content = choices[0].get("message", {}).get("content", "")
        if content:
            yield content

    # ─── health ────────────────────────────────────────────────────

    def health(self) -> HealthSnapshot:
        if self._cfg.via == "proxy":
            return self._health_proxy()
        return self._health_direct()

    def _health_proxy(self) -> HealthSnapshot:
        # Prefer a cheap GET /v1/models — claude-max-proxy (:3456) exposes it and
        # it returns instantly without spending a model inference on every health
        # check. Fall back to a one-token chat for proxies that lack it (e.g. the
        # retired anthropic-proxy :2001, which had no /v1/models).
        try:
            t0 = time.perf_counter_ns()
            r = self._http.get("/v1/models", timeout=HEALTH_TIMEOUT_S)
            latency_ms = (time.perf_counter_ns() - t0) // 1_000_000
            if r.status_code == 200:
                return HealthSnapshot(
                    ok=True, latency_ms=int(latency_ms), quota_remaining=None, detail=None
                )
            if r.status_code != 404:
                return HealthSnapshot(
                    ok=False,
                    latency_ms=int(latency_ms),
                    quota_remaining=None,
                    detail=f"proxy /v1/models returned HTTP {r.status_code}",
                )
            # 404 → proxy has no /v1/models; fall through to the chat probe.
        except Exception as exc:
            return HealthSnapshot(
                ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160]
            )

        # Fallback: a one-token chat completion. Slower (may load the routed
        # model), so allow more headroom than the /v1/models probe.
        try:
            t0 = time.perf_counter_ns()
            r = self._http.post(
                "/v1/chat/completions",
                json={
                    "model": self._cfg.model,
                    "messages": [{"role": "user", "content": "."}],
                    "max_tokens": 1,
                },
                timeout=max(HEALTH_TIMEOUT_S, 20.0),
            )
            latency_ms = (time.perf_counter_ns() - t0) // 1_000_000
            if r.status_code == 200:
                return HealthSnapshot(
                    ok=True, latency_ms=int(latency_ms), quota_remaining=None, detail=None
                )
            return HealthSnapshot(
                ok=False,
                latency_ms=int(latency_ms),
                quota_remaining=None,
                detail=f"proxy returned HTTP {r.status_code}",
            )
        except Exception as exc:
            return HealthSnapshot(
                ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160]
            )

    def _health_direct(self) -> HealthSnapshot:
        try:
            t0 = time.perf_counter_ns()
            self._sdk.models.list(limit=1)
            latency_ms = (time.perf_counter_ns() - t0) // 1_000_000
            return HealthSnapshot(
                ok=True, latency_ms=int(latency_ms), quota_remaining=None, detail=None
            )
        except Exception as exc:
            return HealthSnapshot(
                ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160]
            )

    # ─── cost ──────────────────────────────────────────────────────

    def cost(self, usage: Usage) -> Decimal:
        if self._cfg.via == "proxy":
            # Max subscription is flat-rate — no marginal cost per token.
            return Decimal(0)
        return (
            Decimal(usage.tokens_in) * INPUT_USD_PER_M
            + Decimal(usage.tokens_out) * OUTPUT_USD_PER_M
        ) / Decimal(1_000_000)


# Re-export for tests that imported the constants
__all__ = ["INPUT_USD_PER_M", "OUTPUT_USD_PER_M", "ClaudeProvider"]


# Reference to keep linters from removing the json import once we add streaming
_ = json
