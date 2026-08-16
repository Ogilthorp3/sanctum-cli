"""MlxLocalProvider tests using httpx.MockTransport (no real network)."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from sanctum_cli.config import MlxLocalProvider as MlxLocalProviderConfig
from sanctum_cli.providers.base import ChatOpts, Message, Usage
from sanctum_cli.providers.mlx_local import MlxLocalProvider


def _make_provider(handler: httpx.MockTransport) -> MlxLocalProvider:
    cfg = MlxLocalProviderConfig(
        endpoint="http://test", model="council-secure", timeout_s=2, always_available=True
    )
    p = MlxLocalProvider(cfg)
    # Replace the real client with one wired to the mock transport
    p._client.close()
    p._client = httpx.Client(base_url=cfg.endpoint, transport=handler, timeout=2.0)
    return p


def test_chat_streaming_yields_chunks() -> None:
    sse = "\r\n".join(
        [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo "}}]}',
            'data: {"choices":[{"delta":{"content":"world"}}]}',
            "data: [DONE]",
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["model"] == "council-secure"
        return httpx.Response(200, content=sse, headers={"Content-Type": "text/event-stream"})

    p = _make_provider(httpx.MockTransport(handler))
    chunks = list(p.chat([Message(role="user", content="hi")], ChatOpts(stream=True)))
    assert chunks == ["Hel", "lo ", "world"]


def test_chat_non_streaming_returns_single_chunk() -> None:
    body = {"choices": [{"message": {"content": "full reply"}}]}

    def handler(request: httpx.Request) -> httpx.Response:
        body_in = json.loads(request.content)
        assert body_in["stream"] is False
        return httpx.Response(200, json=body)

    p = _make_provider(httpx.MockTransport(handler))
    chunks = list(p.chat([Message(role="user", content="hi")], ChatOpts(stream=False)))
    assert chunks == ["full reply"]


def test_chat_passes_max_tokens_and_temperature() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    p = _make_provider(httpx.MockTransport(handler))
    list(
        p.chat(
            [Message(role="user", content="x")],
            ChatOpts(stream=False, max_tokens=128, temperature=0.7),
        )
    )
    assert captured["max_tokens"] == 128
    assert captured["temperature"] == pytest.approx(0.7)


def test_health_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "council-secure"}]})

    p = _make_provider(httpx.MockTransport(handler))
    snap = p.health()
    assert snap.ok is True
    assert snap.detail is None


def test_health_failure_carries_detail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "service unavailable"})

    p = _make_provider(httpx.MockTransport(handler))
    snap = p.health()
    assert snap.ok is False
    assert snap.detail is not None
    assert snap.latency_ms is None


def test_health_handles_connection_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    p = _make_provider(httpx.MockTransport(handler))
    snap = p.health()
    assert snap.ok is False
    assert snap.detail is not None


def test_cost_is_zero_for_local() -> None:
    cfg = MlxLocalProviderConfig(endpoint="http://x", model="m", timeout_s=1, always_available=True)
    with patch.object(MlxLocalProvider, "__init__", return_value=None):
        p = MlxLocalProvider.__new__(MlxLocalProvider)
        p._cfg = cfg  # type: ignore[attr-defined]
    from decimal import Decimal

    assert p.cost(Usage(tokens_in=1_000_000, tokens_out=1_000_000)) == Decimal(0)
