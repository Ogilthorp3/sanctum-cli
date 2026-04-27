"""Multimodal-message extension to GeminiProvider — verify attachments
become inline_data parts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sanctum_cli.config import GeminiProvider as GeminiProviderConfig
from sanctum_cli.config import KeychainRef
from sanctum_cli.providers.base import Attachment, ChatOpts, Message
from sanctum_cli.providers.gemini import GeminiProvider


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
        return GeminiProvider(_cfg(), api_key="key")


def test_attachment_becomes_inline_data_part() -> None:
    client = MagicMock()
    client.models.generate_content_stream.return_value = iter([MagicMock(text="ok")])
    p = _make(client)

    att = Attachment(
        kind="image",
        path=Path("ignored.png"),
        mime_type="image/png",
        data=b"\x89PNG-fake-bytes",
    )
    msg = Message(role="user", content="describe", attachments=(att,))

    list(p.chat([msg], ChatOpts(stream=True)))

    contents = client.models.generate_content_stream.call_args.kwargs["contents"]
    assert len(contents) == 1
    parts = contents[0]["parts"]
    # Two parts: the text and the inline_data
    assert any("text" in part and part["text"] == "describe" for part in parts)
    inline = next(part for part in parts if "inline_data" in part)
    assert inline["inline_data"]["mime_type"] == "image/png"
    assert inline["inline_data"]["data"] == b"\x89PNG-fake-bytes"


def test_message_without_attachments_still_works() -> None:
    """Backwards compat — empty attachments tuple."""
    client = MagicMock()
    client.models.generate_content_stream.return_value = iter([MagicMock(text="ok")])
    p = _make(client)
    list(p.chat([Message(role="user", content="hi")], ChatOpts(stream=True)))
    contents = client.models.generate_content_stream.call_args.kwargs["contents"]
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_attachment_text_omitted_when_empty() -> None:
    """If content is empty but attachment present, only the attachment part is sent."""
    client = MagicMock()
    client.models.generate_content_stream.return_value = iter([MagicMock(text="ok")])
    p = _make(client)
    att = Attachment(kind="image", path=Path("x.png"), mime_type="image/png", data=b"x")
    list(p.chat([Message(role="user", content="", attachments=(att,))], ChatOpts(stream=True)))
    contents = client.models.generate_content_stream.call_args.kwargs["contents"]
    parts = contents[0]["parts"]
    assert len(parts) == 1
    assert "inline_data" in parts[0]
