"""ClaudeProvider — Anthropic API via the official SDK."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

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


# Approximate Opus 4.x pricing per 1M tokens (USD). Override per-config in v0.3.
INPUT_USD_PER_M = Decimal("15.00")
OUTPUT_USD_PER_M = Decimal("75.00")


class ClaudeProvider(Provider):
    name = "claude"
    capabilities = (
        Capability.CHAT | Capability.TOOLS | Capability.STREAMING | Capability.THINKING
    )

    def __init__(self, cfg: ClaudeProviderConfig, api_key: str) -> None:
        from anthropic import Anthropic

        self._cfg = cfg
        self._client = Anthropic(
            api_key=api_key,
            base_url=cfg.endpoint,
            timeout=float(cfg.timeout_s),
            max_retries=cfg.max_retries,
        )

    def _split_system(self, messages: list[Message]) -> tuple[str, list[Message]]:
        system_parts = [m.content for m in messages if m.role == "system"]
        chat_msgs = [m for m in messages if m.role != "system"]
        return "\n\n".join(system_parts), chat_msgs

    def _to_anthropic(self, messages: list[Message]) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def chat(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
        system, chat_msgs = self._split_system(messages)
        anth_msgs = cast("Any", self._to_anthropic(chat_msgs))
        max_tokens = opts.max_tokens or self._cfg.max_tokens
        extra: dict[str, Any] = {}
        if system:
            extra["system"] = system
        if opts.temperature is not None:
            extra["temperature"] = opts.temperature

        if opts.stream:
            with self._client.messages.stream(
                model=self._cfg.model,
                messages=anth_msgs,
                max_tokens=max_tokens,
                **extra,
            ) as stream:
                yield from stream.text_stream
            return

        response = self._client.messages.create(
            model=self._cfg.model,
            messages=anth_msgs,
            max_tokens=max_tokens,
            **extra,
        )
        for block in response.content:
            if block.type == "text":
                yield block.text

    def health(self) -> HealthSnapshot:
        try:
            t0 = time.perf_counter_ns()
            self._client.models.list(limit=1)
            latency_ms = (time.perf_counter_ns() - t0) // 1_000_000
            return HealthSnapshot(ok=True, latency_ms=int(latency_ms), quota_remaining=None, detail=None)
        except Exception as exc:  # SDK can raise many specific types; surface the message
            return HealthSnapshot(
                ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160]
            )

    def cost(self, usage: Usage) -> Decimal:
        return (
            Decimal(usage.tokens_in) * INPUT_USD_PER_M
            + Decimal(usage.tokens_out) * OUTPUT_USD_PER_M
        ) / Decimal(1_000_000)
