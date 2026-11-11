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


def test_direct_mode_requires_api_key() -> None:
    import pytest

    with pytest.raises(ValueError, match="requires an API key"):
        GeminiProvider(_cfg(), api_key=None)


# ─── proxy mode (Gemini subscription via local OpenAI-compatible bridge) ───


def _proxy_cfg(**overrides: object) -> GeminiProviderConfig:
    base: dict[str, object] = {
        "via": "proxy",
        "proxy_url": "http://127.0.0.1:6543",
        "timeout_s": 120,
        "max_retries": 2,
    }
    base.update(overrides)
    return GeminiProviderConfig.model_validate(base)


def _make_proxy(handler: object, **cfg_overrides: object) -> GeminiProvider:
    import httpx

    cfg = _proxy_cfg(**cfg_overrides)
    p = GeminiProvider(cfg, api_key=None)
    assert p._http is not None
    base_url = str(p._http.base_url)
    p._http.close()
    p._http = httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return p


def test_proxy_mode_does_not_require_api_key() -> None:
    p = GeminiProvider(_proxy_cfg(), api_key=None)
    try:
        assert p._client is None
        assert p._http is not None
    finally:
        p.close()


def test_make_provider_gemini_proxy_skips_keychain() -> None:
    from sanctum_cli.config import Providers
    from sanctum_cli.providers.registry import make_provider

    cfg = Providers(gemini=_proxy_cfg())
    with patch(
        "sanctum_cli.providers.registry.keychain.read",
        side_effect=AssertionError("keychain must not be read when via=proxy"),
    ):
        p = make_provider("gemini", cfg)
    try:
        assert isinstance(p, GeminiProvider)
    finally:
        p.close()


def test_make_provider_gemini_direct_reads_keychain() -> None:
    from sanctum_cli.config import Providers
    from sanctum_cli.providers.registry import make_provider

    cfg = Providers()
    with (
        patch("sanctum_cli.providers.registry.keychain.read", return_value="gm-key") as kc,
        patch("google.genai.Client", return_value=MagicMock()),
    ):
        p = make_provider("gemini", cfg)
    kc.assert_called_once_with(account="sanctum", service="google-ai-api-key")
    p.close()


def test_proxy_health_ok_checks_status_and_authenticated() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/health"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "authenticated": True,
                "best_model": "Gemini 4 Carbon (Medium)",
            },
        )

    p = _make_proxy(handler)
    try:
        snap = p.health()
        assert snap.ok is True
        assert snap.latency_ms is not None and snap.latency_ms >= 0
        assert snap.detail is None
    finally:
        p.close()


def test_proxy_health_fails_when_not_authenticated() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "authenticated": False})

    p = _make_proxy(handler)
    try:
        snap = p.health()
        assert snap.ok is False
        assert snap.detail is not None
        assert "authenticated=False" in snap.detail
    finally:
        p.close()


def test_proxy_health_fails_when_status_not_ok() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "degraded", "authenticated": True})

    p = _make_proxy(handler)
    try:
        snap = p.health()
        assert snap.ok is False
        assert snap.detail is not None
        assert "status=degraded" in snap.detail
    finally:
        p.close()


def test_proxy_health_fails_on_http_503_with_error_detail() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "status": "auth_required",
                "authenticated": False,
                "error": "agy reports signed out",
            },
        )

    p = _make_proxy(handler)
    try:
        snap = p.health()
        assert snap.ok is False
        assert snap.detail is not None
        assert "503" in snap.detail
        assert "signed out" in snap.detail
    finally:
        p.close()


def test_proxy_health_transport_error_captured() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    p = _make_proxy(handler)
    try:
        snap = p.health()
        assert snap.ok is False
        assert snap.latency_ms is None
        assert snap.detail is not None
        assert "connection refused" in snap.detail
    finally:
        p.close()


def test_proxy_cost_is_zero() -> None:
    p = GeminiProvider(_proxy_cfg(), api_key=None)
    try:
        assert p.cost(Usage(tokens_in=1_000_000, tokens_out=2_000_000)) == Decimal("0")
    finally:
        p.close()


def test_proxy_chat_non_streaming_maps_default_model_and_posts_openai() -> None:
    import json as _json

    import httpx

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]},
        )

    p = _make_proxy(handler)
    try:
        chunks = list(
            p.chat(
                [
                    Message(role="system", content="Be terse."),
                    Message(role="user", content="ping"),
                ],
                ChatOpts(stream=False, max_tokens=64, temperature=0.2),
            )
        )
        assert chunks == ["pong"]
        assert str(captured["url"]) == "http://127.0.0.1:6543/v1/chat/completions"
        body = captured["body"]
        assert body["model"] == "gemini-best-pro"  # type: ignore[index]
        assert body["stream"] is False  # type: ignore[index]
        assert body["max_tokens"] == 64  # type: ignore[index]
        assert body["temperature"] == 0.2  # type: ignore[index]
        assert body["messages"] == [  # type: ignore[index]
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "ping"},
        ]
    finally:
        p.close()


def test_proxy_chat_preserves_custom_model_and_endpoint_override() -> None:
    import json as _json

    import httpx

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    p = _make_proxy(
        handler,
        endpoint="http://127.0.0.1:16543",
        model="Gemini 4 Carbon (High)",
    )
    try:
        chunks = list(p.chat([Message(role="user", content="hi")], ChatOpts(stream=False)))
        assert chunks == ["ok"]
        assert str(captured["url"]) == "http://127.0.0.1:16543/v1/chat/completions"
        assert captured["body"]["model"] == "Gemini 4 Carbon (High)"  # type: ignore[index]
    finally:
        p.close()


def test_proxy_chat_streaming_sse_yields_chunks() -> None:
    import httpx

    sse_payload = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"alpha"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" beta"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse_payload,
        )

    p = _make_proxy(handler)
    try:
        chunks = list(p.chat([Message(role="user", content="x")], ChatOpts(stream=True)))
        assert chunks == ["alpha", " beta"]
    finally:
        p.close()


def test_proxy_chat_streaming_handles_non_sse_json_response() -> None:
    """sanctum-agy-proxy returns a single JSON completion even when stream=True."""
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "full reply"}}]},
        )

    p = _make_proxy(handler)
    try:
        chunks = list(p.chat([Message(role="user", content="x")], ChatOpts(stream=True)))
        assert chunks == ["full reply"]
    finally:
        p.close()


def test_proxy_chat_multimodal_attachments_encoded_as_image_url() -> None:
    import base64
    import json as _json
    from pathlib import Path

    import httpx

    from sanctum_cli.providers.base import Attachment

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "seen"}}]},
        )

    p = _make_proxy(handler)
    try:
        raw_png = b"\x89PNG-fake"
        att = Attachment(kind="image", path=Path("x.png"), mime_type="image/png", data=raw_png)
        chunks = list(
            p.chat(
                [Message(role="user", content="describe", attachments=(att,))],
                ChatOpts(stream=False),
            )
        )
        assert chunks == ["seen"]
        msgs = captured["body"]["messages"]  # type: ignore[index]
        expected_uri = f"data:image/png;base64,{base64.b64encode(raw_png).decode('ascii')}"
        assert msgs == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": expected_uri}},
                ],
            }
        ]
    finally:
        p.close()
