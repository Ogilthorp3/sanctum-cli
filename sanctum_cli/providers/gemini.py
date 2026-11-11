"""GeminiProvider — two flavours, one interface.

``via=direct`` (default) uses the ``google-genai`` SDK against Google AI
Studio and bills the API key in Keychain.

``via=proxy`` talks to a local OpenAI-compatible Gemini subscription bridge
(``sanctum-agy-proxy`` on ``proxy_url``, default ``http://127.0.0.1:6543``);
billing flows through the subscription with zero marginal API cost.
"""

from __future__ import annotations

import base64
import contextlib
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

    from sanctum_cli.config import GeminiProvider as GeminiProviderConfig


# Approximate 2.5 Pro pricing per 1M tokens (USD). Override per-config in v0.3.
INPUT_USD_PER_M = Decimal("1.25")
OUTPUT_USD_PER_M = Decimal("10.00")
DEFAULT_DIRECT_ENDPOINT = "https://generativelanguage.googleapis.com"
DEFAULT_DIRECT_MODEL = "gemini-2.5-pro"
DEFAULT_PROXY_URL = "http://127.0.0.1:6543"
DEFAULT_PROXY_MODEL = "gemini-best-pro"
HEALTH_TIMEOUT_S = 5.0


class GeminiProvider(Provider):
    name = "gemini"
    capabilities = Capability.CHAT | Capability.VISION | Capability.STREAMING

    def __init__(self, cfg: GeminiProviderConfig, api_key: str | None = None) -> None:
        self._cfg = cfg
        self._api_key = api_key
        if cfg.via == "direct":
            if not api_key:
                msg = "via=direct requires an API key from Keychain"
                raise ValueError(msg)
            from google import genai
            from google.genai import types as genai_types

            self._client: Any = genai.Client(
                api_key=api_key,
                http_options=genai_types.HttpOptions(timeout=int(cfg.timeout_s * 1000)),
            )
            self._http: httpx.Client | None = None
        else:
            self._client = None
            self._http = httpx.Client(
                base_url=self._resolve_proxy_url(cfg),
                timeout=float(cfg.timeout_s),
            )

    @staticmethod
    def _resolve_proxy_url(cfg: GeminiProviderConfig) -> str:
        if cfg.proxy_url != DEFAULT_PROXY_URL:
            return cfg.proxy_url
        if cfg.endpoint != DEFAULT_DIRECT_ENDPOINT:
            return cfg.endpoint
        return cfg.proxy_url

    def _proxy_model(self) -> str:
        if self._cfg.model == DEFAULT_DIRECT_MODEL:
            return DEFAULT_PROXY_MODEL
        return self._cfg.model

    @staticmethod
    def _gemini_role(role: str) -> str:
        # Gemini uses 'model' rather than 'assistant'; 'system' is hoisted out
        return "user" if role == "user" else "model"

    def _split_system(self, messages: list[Message]) -> tuple[str | None, list[Message]]:
        system_parts = [m.content for m in messages if m.role == "system"]
        chat_msgs = [m for m in messages if m.role != "system"]
        return ("\n\n".join(system_parts) or None), chat_msgs

    def _to_contents(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            parts: list[dict[str, Any]] = [{"text": m.content}] if m.content else []
            for att in m.attachments:
                parts.append(
                    {"inline_data": {"mime_type": att.mime_type, "data": att.read_bytes()}}
                )
            out.append({"role": self._gemini_role(m.role), "parts": parts})
        return out

    @staticmethod
    def _to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if not m.attachments:
                out.append({"role": m.role, "content": m.content})
                continue
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"type": "text", "text": m.content})
            for att in m.attachments:
                encoded = base64.b64encode(att.read_bytes()).decode("ascii")
                data_uri = f"data:{att.mime_type};base64,{encoded}"
                parts.append({"type": "image_url", "image_url": {"url": data_uri}})
            out.append({"role": m.role, "content": parts})
        return out

    def _build_config(self, system: str | None, opts: ChatOpts) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        if system is not None:
            cfg["system_instruction"] = system
        if opts.max_tokens is not None:
            cfg["max_output_tokens"] = opts.max_tokens
        if opts.temperature is not None:
            cfg["temperature"] = opts.temperature
        return cfg

    # ─── chat ──────────────────────────────────────────────────────

    def chat(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
        if self._cfg.via == "direct":
            yield from self._chat_direct(messages, opts)
        else:
            yield from self._chat_proxy(messages, opts)

    def _chat_direct(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
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

    @staticmethod
    def _extract_completion_text(data: Any) -> Iterator[str]:
        if not isinstance(data, dict):
            return
        choices = data.get("choices") or []
        if not choices:
            return
        msg = choices[0].get("message") or {}
        content = msg.get("content", "")
        if content:
            yield str(content)

    def _chat_proxy(self, messages: list[Message], opts: ChatOpts) -> Iterator[str]:
        assert self._http is not None
        body: dict[str, Any] = {
            "model": self._proxy_model(),
            "messages": self._to_openai_messages(messages),
            "stream": opts.stream,
        }
        if opts.max_tokens is not None:
            body["max_tokens"] = opts.max_tokens
        if opts.temperature is not None:
            body["temperature"] = opts.temperature

        if not opts.stream:
            response = self._http.post("/v1/chat/completions", json=body)
            response.raise_for_status()
            yield from self._extract_completion_text(response.json())
            return

        with self._http.stream("POST", "/v1/chat/completions", json=body) as response:
            response.raise_for_status()
            saw_sse = False
            non_sse_lines: list[str] = []
            for line in response.iter_lines():
                stripped = line.strip()
                if not stripped or stripped.startswith(":"):
                    continue
                if stripped.startswith("data:"):
                    saw_sse = True
                    payload = stripped[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    first = choices[0]
                    delta = first.get("delta") or {}
                    text = delta.get("content")
                    if text is None:
                        text = (first.get("message") or {}).get("content")
                    if text:
                        yield str(text)
                elif not saw_sse:
                    non_sse_lines.append(line)

            if not saw_sse and non_sse_lines:
                try:
                    data = json.loads("\n".join(non_sse_lines))
                except json.JSONDecodeError:
                    return
                yield from self._extract_completion_text(data)

    # ─── health ────────────────────────────────────────────────────

    def health(self) -> HealthSnapshot:
        if self._cfg.via == "proxy":
            return self._health_proxy()
        return self._health_direct()

    def _health_proxy(self) -> HealthSnapshot:
        assert self._http is not None
        try:
            t0 = time.perf_counter_ns()
            r = self._http.get("/health", timeout=HEALTH_TIMEOUT_S)
            latency_ms = int((time.perf_counter_ns() - t0) // 1_000_000)
            if r.status_code != 200:
                err_msg = ""
                with contextlib.suppress(ValueError):
                    payload = r.json()
                    if isinstance(payload, dict) and payload.get("error"):
                        err_msg = f": {payload['error']}"
                return HealthSnapshot(
                    ok=False,
                    latency_ms=latency_ms,
                    quota_remaining=None,
                    detail=f"proxy /health returned HTTP {r.status_code}{err_msg}"[:160],
                )
            data = r.json()
            if (
                isinstance(data, dict)
                and data.get("status") == "ok"
                and data.get("authenticated") is True
            ):
                return HealthSnapshot(
                    ok=True,
                    latency_ms=latency_ms,
                    quota_remaining=None,
                    detail=None,
                )
            detail = (
                str(data.get("error"))
                if isinstance(data, dict) and data.get("error")
                else (
                    f"proxy /health status={data.get('status') if isinstance(data, dict) else 'invalid'} "
                    f"authenticated={data.get('authenticated') if isinstance(data, dict) else None}"
                )
            )
            return HealthSnapshot(
                ok=False,
                latency_ms=latency_ms,
                quota_remaining=None,
                detail=detail[:160],
            )
        except Exception as exc:
            return HealthSnapshot(
                ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160]
            )

    def _health_direct(self) -> HealthSnapshot:
        try:
            t0 = time.perf_counter_ns()
            # Listing models is the cheapest auth probe Google exposes
            iterator = self._client.models.list()
            next(iter(iterator), None)
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
            return Decimal(0)
        return (
            Decimal(usage.tokens_in) * INPUT_USD_PER_M
            + Decimal(usage.tokens_out) * OUTPUT_USD_PER_M
        ) / Decimal(1_000_000)

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        with contextlib.suppress(Exception):
            self.close()


__all__ = [
    "DEFAULT_PROXY_MODEL",
    "DEFAULT_PROXY_URL",
    "INPUT_USD_PER_M",
    "OUTPUT_USD_PER_M",
    "GeminiProvider",
]
