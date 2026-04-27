"""GeminiProvider — Google AI Studio API via google-genai SDK."""

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

    from sanctum_cli.config import GeminiProvider as GeminiProviderConfig


# Approximate 2.5 Pro pricing per 1M tokens (USD). Override per-config in v0.3.
INPUT_USD_PER_M = Decimal("1.25")
OUTPUT_USD_PER_M = Decimal("10.00")


class GeminiProvider(Provider):
    name = "gemini"
    capabilities = Capability.CHAT | Capability.VISION | Capability.STREAMING

    def __init__(self, cfg: GeminiProviderConfig, api_key: str) -> None:
        from google import genai

        self._cfg = cfg
        self._client = genai.Client(api_key=api_key)

    @staticmethod
    def _gemini_role(role: str) -> str:
        # Gemini uses 'model' rather than 'assistant'; 'system' is hoisted out
        return "user" if role == "user" else "model"

    def _split_system(self, messages: list[Message]) -> tuple[str | None, list[Message]]:
        system_parts = [m.content for m in messages if m.role == "system"]
        chat_msgs = [m for m in messages if m.role != "system"]
        return ("\n\n".join(system_parts) or None), chat_msgs

    def _to_contents(self, messages: list[Message]) -> list[dict[str, Any]]:
        return [
            {"role": self._gemini_role(m.role), "parts": [{"text": m.content}]} for m in messages
        ]

    def _build_config(self, system: str | None, opts: ChatOpts) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        if system is not None:
            cfg["system_instruction"] = system
        if opts.max_tokens is not None:
            cfg["max_output_tokens"] = opts.max_tokens
        if opts.temperature is not None:
            cfg["temperature"] = opts.temperature
        return cfg

    def chat(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
        system, chat_msgs = self._split_system(messages)
        contents = self._to_contents(chat_msgs)
        config = self._build_config(system, opts)

        if opts.stream:
            for chunk in self._client.models.generate_content_stream(
                model=self._cfg.model, contents=contents, config=cast("Any", config or None)
            ):
                text = getattr(chunk, "text", None)
                if text:
                    yield text
            return

        response = self._client.models.generate_content(
            model=self._cfg.model, contents=contents, config=cast("Any", config or None)
        )
        text = getattr(response, "text", None)
        if text:
            yield text

    def health(self) -> HealthSnapshot:
        try:
            t0 = time.perf_counter_ns()
            # Listing models is the cheapest auth probe Google exposes
            iterator = self._client.models.list()
            next(iter(iterator), None)
            latency_ms = (time.perf_counter_ns() - t0) // 1_000_000
            return HealthSnapshot(ok=True, latency_ms=int(latency_ms), quota_remaining=None, detail=None)
        except Exception as exc:
            return HealthSnapshot(
                ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160]
            )

    def cost(self, usage: Usage) -> Decimal:
        return (
            Decimal(usage.tokens_in) * INPUT_USD_PER_M
            + Decimal(usage.tokens_out) * OUTPUT_USD_PER_M
        ) / Decimal(1_000_000)
