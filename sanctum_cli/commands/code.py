"""``sanctum code "..."`` — forced Claude routing."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - Typer evaluates the annotation at decoration time
from typing import Annotated

import typer

from sanctum_cli.commands import chat as chat_cmd


def code_command(
    prompt: Annotated[str | None, typer.Argument(help="Prompt text. Omit to read from stdin.")] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f", help="Read prompt from a file."),
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
    """Send a coding-oriented prompt — forces routing to Claude.

    Equivalent to ``sanctum chat -p claude "..."`` but the intent is logged
    as ``code`` in telemetry and the route rule shows ``intent.code``.
    """
    chat_cmd.chat_command(
        prompt=prompt,
        provider="claude",
        file=file,
        no_stream=no_stream,
        max_tokens=max_tokens,
        temperature=temperature,
    )
