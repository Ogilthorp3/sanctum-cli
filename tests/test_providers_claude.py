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
