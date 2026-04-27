"""``sanctum vision FILE "..."`` — multimodal Gemini routing.

Reads the file, auto-detects mime type, attaches it to the user message,
forces Gemini, and streams the response. Capability-gated: refuses to
dispatch to a provider without ``Capability.VISION``.
"""

from __future__ import annotations

import mimetypes
import sys
from pathlib import Path  # noqa: TC003 - Typer evaluates the annotation at decoration time
from typing import Annotated

import typer

from sanctum_cli import config, telemetry
from sanctum_cli.errors import ProviderError, UserError
from sanctum_cli.providers import (
    Attachment,
    Capability,
    ChatOpts,
    Message,
    make_provider,
)

DEFAULT_MIME = "application/octet-stream"
SUPPORTED_KINDS: dict[str, str] = {
    "image/": "image",
    "video/": "video",
}


def _detect(path: Path) -> tuple[str, str]:
    """Return (kind, mime). kind is image | video | file."""
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or DEFAULT_MIME
    for prefix, kind in SUPPORTED_KINDS.items():
        if mime.startswith(prefix):
            return kind, mime
    return "file", mime


def vision_command(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    prompt: Annotated[
        str | None,
        typer.Argument(help="Prompt text. Defaults to 'Describe this in detail.' if omitted."),
    ] = None,
    no_stream: Annotated[
        bool, typer.Option("--no-stream", help="Wait for full response before printing.")
    ] = False,
    max_tokens: Annotated[
        int | None,
        typer.Option("--max-tokens", "-t", help="Cap response length.", min=1),
    ] = None,
    temperature: Annotated[
        float | None,
        typer.Option("--temperature", help="Sampling temperature 0.0..2.0.", min=0.0, max=2.0),
    ] = None,
) -> None:
    """Send an image / video / file to Gemini with a prompt."""
    text = prompt or "Describe this in detail."
    kind, mime = _detect(file)
    attachment = Attachment(kind=kind, path=file, mime_type=mime)  # type: ignore[arg-type]

    cfg = config.load()
    p = make_provider("gemini", cfg.cli.providers)
    if Capability.VISION not in p.capabilities:
        msg = "selected provider lacks VISION capability"
        raise UserError(msg, fix="ensure cli.routing.fallback or -p targets a vision-capable provider")

    opts = ChatOpts(stream=not no_stream, max_tokens=max_tokens, temperature=temperature)
    messages = [Message(role="user", content=text, attachments=(attachment,))]

    with telemetry.Span(cfg.cli.telemetry, command="vision") as span:
        span.set(
            provider="gemini",
            route_rule="intent.vision",
            intent="vision",
            prompt=text,
            extra={"file": str(file), "mime": mime, "kind": kind},
        )
        try:
            for chunk in p.chat(messages, opts):
                sys.stdout.write(chunk)
                sys.stdout.flush()
        except Exception as exc:
            msg = f"gemini call failed: {type(exc).__name__}: {exc}"
            raise ProviderError(msg, fix="run `sanctum doctor` to probe provider health") from exc

    sys.stdout.write("\n")
    sys.stdout.flush()
