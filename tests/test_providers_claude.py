"""ClaudeProvider tests — anthropic SDK is patched at the boundary."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from sanctum_cli.config import ClaudeProvider as ClaudeProviderConfig
from sanctum_cli.config import KeychainRef
from sanctum_cli.providers.base import ChatOpts, Message, Usage
from sanctum_cli.providers.claude import (
    INPUT_USD_PER_M,
    OUTPUT_USD_PER_M,
    ClaudeProvider,
)


def _cfg() -> ClaudeProviderConfig:
    return ClaudeProviderConfig(
        via="direct",
        endpoint="https://api.anthropic.com",
        model="claude-opus-4-7",
        keychain=KeychainRef(service="anthropic-api-key", account="sanctum"),
        timeout_s=120,
        max_retries=2,
        max_tokens=4096,
    )


def _make(client_mock: MagicMock) -> ClaudeProvider:
    with patch("anthropic.Anthropic", return_value=client_mock):
        return ClaudeProvider(_cfg(), api_key="sk-test")


def test_chat_streaming_pulls_text_from_text_stream() -> None:
    client = MagicMock()
    stream_ctx = MagicMock()
    stream_ctx.__enter__ = MagicMock(return_value=stream_ctx)
    stream_ctx.__exit__ = MagicMock(return_value=False)
    stream_ctx.text_stream = iter(["alpha", " beta"])
    client.messages.stream.return_value = stream_ctx

    p = _make(client)
    chunks = list(p.chat([Message(role="user", content="hi")], ChatOpts(stream=True)))
    assert chunks == ["alpha", " beta"]
    client.messages.stream.assert_called_once()
    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["model"] == "claude-opus-4-7"
    assert kwargs["max_tokens"] == 4096
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_separates_system_messages() -> None:
    client = MagicMock()
    stream_ctx = MagicMock()
    stream_ctx.__enter__ = MagicMock(return_value=stream_ctx)
    stream_ctx.__exit__ = MagicMock(return_value=False)
    stream_ctx.text_stream = iter([""])
    client.messages.stream.return_value = stream_ctx

    p = _make(client)
    list(
        p.chat(
            [
                Message(role="system", content="You are sanctum."),
                Message(role="system", content="Be terse."),
                Message(role="user", content="ok"),
            ],
            ChatOpts(stream=True),
        )
    )
    kwargs = client.messages.stream.call_args.kwargs
    assert "system" in kwargs and "sanctum" in kwargs["system"] and "terse" in kwargs["system"]
    assert kwargs["messages"] == [{"role": "user", "content": "ok"}]


def test_chat_non_streaming_collects_text_blocks() -> None:
    client = MagicMock()
    block_text = MagicMock()
    block_text.type = "text"
    block_text.text = "answer"
    block_other = MagicMock()
    block_other.type = "tool_use"
    response = MagicMock()
    response.content = [block_text, block_other]
    client.messages.create.return_value = response

    p = _make(client)
    chunks = list(p.chat([Message(role="user", content="x")], ChatOpts(stream=False)))
    assert chunks == ["answer"]
    client.messages.create.assert_called_once()


def test_health_records_latency_on_success() -> None:
    client = MagicMock()
    client.models.list.return_value = MagicMock()  # any non-raising return
    p = _make(client)
    snap = p.health()
    assert snap.ok is True
    assert snap.latency_ms is not None
    assert snap.latency_ms >= 0


def test_health_carries_error_detail_on_failure() -> None:
    client = MagicMock()
    client.models.list.side_effect = RuntimeError("auth failed")
    p = _make(client)
    snap = p.health()
    assert snap.ok is False
    assert snap.detail is not None
    assert "auth failed" in snap.detail


def test_cost_uses_published_rates() -> None:
    p = _make(MagicMock())
    expected: Any = (
        Decimal(1_000_000) * INPUT_USD_PER_M + Decimal(2_000_000) * OUTPUT_USD_PER_M
    ) / Decimal(1_000_000)
    assert p.cost(Usage(tokens_in=1_000_000, tokens_out=2_000_000)) == expected


# ─── proxy mode (Max subscription via claude-cli-proxy) ─────────────


def _proxy_cfg() -> ClaudeProviderConfig:
    return ClaudeProviderConfig(
        via="proxy",
        endpoint="http://127.0.0.1:2001",
        model="claude-opus-4-7",
        keychain=KeychainRef(service="anthropic-api-key", account="sanctum"),
        timeout_s=300,
        max_retries=2,
        max_tokens=4096,
    )


def _make_proxy() -> ClaudeProvider:
    return ClaudeProvider(_proxy_cfg(), api_key=None)


def test_proxy_chat_posts_openai_format() -> None:
    import httpx

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]},
        )

    p = _make_proxy()
    p._http.close()
    p._http = httpx.Client(base_url=p._cfg.endpoint, transport=httpx.MockTransport(handler))

    chunks = list(p.chat([Message(role="user", content="ping")], ChatOpts(stream=False)))
    assert chunks == ["pong"]
    body = captured["body"]
    assert body["model"] == "claude-opus-4-7"  # type: ignore[index]
    assert body["messages"] == [{"role": "user", "content": "ping"}]  # type: ignore[index]
    assert body["stream"] is False  # proxy does not stream  # type: ignore[index]
    assert "/v1/chat/completions" in str(captured["url"])


def test_proxy_chat_streaming_flag_ignored_yields_full_chunk() -> None:
    """Even when caller passes stream=True, proxy returns a single chunk."""
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "full reply"}}]},
        )

    p = _make_proxy()
    p._http.close()
    p._http = httpx.Client(base_url=p._cfg.endpoint, transport=httpx.MockTransport(handler))

    chunks = list(p.chat([Message(role="user", content="x")], ChatOpts(stream=True)))
    assert chunks == ["full reply"]


def test_proxy_cost_is_zero_max_subscription_is_flat_rate() -> None:
    p = _make_proxy()
    assert p.cost(Usage(tokens_in=1_000_000, tokens_out=1_000_000)) == Decimal(0)


def test_proxy_health_pings_models_endpoint() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        # Health probe prefers a cheap GET /v1/models — no inference spent.
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "claude-opus-4"}]})

    p = _make_proxy()
    p._http.close()
    p._http = httpx.Client(base_url=p._cfg.endpoint, transport=httpx.MockTransport(handler))

    snap = p.health()
    assert snap.ok is True
    assert snap.latency_ms is not None


def test_proxy_health_falls_back_to_chat_when_no_models_endpoint() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(404, text="not found")
        # Older proxies (retired :2001) lack /v1/models → fall back to chat.
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "."}}]})

    p = _make_proxy()
    p._http.close()
    p._http = httpx.Client(base_url=p._cfg.endpoint, transport=httpx.MockTransport(handler))

    snap = p.health()
    assert snap.ok is True


def test_proxy_health_failure_carries_detail() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="proxy down")

    p = _make_proxy()
    p._http.close()
    p._http = httpx.Client(base_url=p._cfg.endpoint, transport=httpx.MockTransport(handler))

    snap = p.health()
    assert snap.ok is False
    assert snap.detail is not None
    assert "503" in snap.detail


def test_proxy_mode_does_not_require_api_key() -> None:
    """Constructor succeeds without an API key when via=proxy."""
    cfg = _proxy_cfg()
    p = ClaudeProvider(cfg, api_key=None)
    assert p._sdk is None  # no SDK instantiated


def test_direct_mode_requires_api_key() -> None:
    """Constructor raises when via=direct and no key supplied."""
    cfg = ClaudeProviderConfig(
        via="direct",
        endpoint="https://api.anthropic.com",
        model="claude-opus-4-7",
        keychain=KeychainRef(service="anthropic-api-key", account="sanctum"),
        timeout_s=120,
        max_retries=2,
        max_tokens=4096,
    )
    import pytest

    with pytest.raises(ValueError, match="requires an API key"):
        ClaudeProvider(cfg, api_key=None)
