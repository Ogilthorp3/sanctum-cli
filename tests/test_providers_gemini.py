"""GeminiProvider tests — google-genai client is patched at the boundary."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from sanctum_cli.config import GeminiProvider as GeminiProviderConfig
from sanctum_cli.config import KeychainRef
from sanctum_cli.providers.base import ChatOpts, Message, Usage
from sanctum_cli.providers.gemini import (
    INPUT_USD_PER_M,
    OUTPUT_USD_PER_M,
    GeminiProvider,
)


def _cfg() -> GeminiProviderConfig:
    return GeminiProviderConfig(
        endpoint="https://generativelanguage.googleapis.com",
        model="gemini-2.5-pro",
        keychain=KeychainRef(service="google-ai-api-key", account="sanctum"),
        timeout_s=120,
        max_retries=2,
    )


def _make(client_mock: MagicMock) -> GeminiProvider:
    with patch("google.genai.Client", return_value=client_mock):
        return GeminiProvider(_cfg(), api_key="key-test")


def test_chat_streaming_yields_text_from_chunks() -> None:
    client = MagicMock()
    chunk_a = MagicMock(text="alpha")
    chunk_b = MagicMock(text=" beta")
    chunk_empty = MagicMock(text=None)
    client.models.generate_content_stream.return_value = iter([chunk_a, chunk_empty, chunk_b])

    p = _make(client)
    chunks = list(p.chat([Message(role="user", content="x")], ChatOpts(stream=True)))
    assert chunks == ["alpha", " beta"]
    kwargs = client.models.generate_content_stream.call_args.kwargs
    assert kwargs["model"] == "gemini-2.5-pro"
    assert kwargs["contents"] == [{"role": "user", "parts": [{"text": "x"}]}]


def test_chat_assistant_role_translated_to_model() -> None:
    client = MagicMock()
    client.models.generate_content_stream.return_value = iter([MagicMock(text="")])
    p = _make(client)
    list(
        p.chat(
            [
                Message(role="user", content="hi"),
                Message(role="assistant", content="hello"),
                Message(role="user", content="continue"),
            ],
            ChatOpts(stream=True),
        )
    )
    contents = client.models.generate_content_stream.call_args.kwargs["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]


def test_chat_non_streaming_returns_response_text() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text="answer")
    p = _make(client)
    chunks = list(p.chat([Message(role="user", content="x")], ChatOpts(stream=False)))
    assert chunks == ["answer"]


def test_chat_system_message_lifted_to_system_instruction() -> None:
    client = MagicMock()
    client.models.generate_content_stream.return_value = iter([MagicMock(text="")])
    p = _make(client)
    list(
        p.chat(
            [Message(role="system", content="Be terse."), Message(role="user", content="hi")],
            ChatOpts(stream=True),
        )
    )
    config_arg = client.models.generate_content_stream.call_args.kwargs["config"]
    assert config_arg is not None
    assert config_arg["system_instruction"] == "Be terse."


def test_chat_passes_max_tokens_and_temperature() -> None:
    client = MagicMock()
    client.models.generate_content_stream.return_value = iter([MagicMock(text="")])
    p = _make(client)
    list(
        p.chat(
            [Message(role="user", content="x")],
            ChatOpts(stream=True, max_tokens=256, temperature=0.5),
        )
    )
    config_arg = client.models.generate_content_stream.call_args.kwargs["config"]
    assert config_arg["max_output_tokens"] == 256
    assert config_arg["temperature"] == 0.5


def test_health_ok() -> None:
    client = MagicMock()
    client.models.list.return_value = iter([MagicMock()])
    p = _make(client)
    snap = p.health()
    assert snap.ok is True
    assert snap.latency_ms is not None


def test_health_failure_carries_detail() -> None:
    client = MagicMock()
    client.models.list.side_effect = RuntimeError("blocked")
    p = _make(client)
    snap = p.health()
    assert snap.ok is False
    assert snap.detail is not None
    assert "blocked" in snap.detail


def test_cost_uses_published_rates() -> None:
    p = _make(MagicMock())
    expected = (
        Decimal(1_000_000) * INPUT_USD_PER_M + Decimal(1_000_000) * OUTPUT_USD_PER_M
    ) / Decimal(1_000_000)
    assert p.cost(Usage(tokens_in=1_000_000, tokens_out=1_000_000)) == expected
