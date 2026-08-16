"""``sanctum chat "..."`` — first dispatch through the router.

Streams the response chunk-by-chunk to stdout so the operator sees
the model thinking. Telemetry Span captures provider, route rule, and
duration; error events go to the same JSONL.

Attachment-aware routing lands in v0.3 (vision/code subcommands lift
hints up; this v0.2 chat is text-only).
"""

from __future__ import annotations

import sys
from pathlib import Path  # noqa: TC003 - Typer evaluates the annotation at decoration time
from typing import Annotated, Literal, cast

import typer

from sanctum_cli import config, telemetry
from sanctum_cli.errors import ProviderError, UserError
from sanctum_cli.haus import haus_required
from sanctum_cli.providers import ChatOpts, Message, make_provider
from sanctum_cli.router import Flags, Intent, route

ProviderName = Literal["claude", "gemini", "mlx_local"]
_VALID = frozenset({"claude", "gemini", "mlx_local"})


def chat_command(
    prompt: Annotated[
        str | None, typer.Argument(help="Prompt text. Omit to read from stdin.")
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "-p",
            help="Force a provider: claude | gemini | mlx_local. Skips routing.",
        ),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f", help="Read prompt from a file."),
    ] = None,
    no_stream: Annotated[
        bool, typer.Option("--no-stream", help="Wait for full response before printing.")
    ] = False,
    max_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-tokens", "-t", help="Cap response length (default: provider config max).", min=1
        ),
    ] = None,
    temperature: Annotated[
        float | None,
        typer.Option("--temperature", help="Sampling temperature 0.0..2.0.", min=0.0, max=2.0),
    ] = None,
) -> None:
    """Send a prompt; the router picks the model unless ``--provider`` overrides."""
    haus_required("council")
    text = _resolve_prompt(prompt, file)
    cfg = config.load()

    decision = route(
        Intent(kind="chat"),
        attachments=[],
        flags=Flags(provider=_validate_provider(provider)),
        cfg=cfg.cli,
    )

    if provider is not None and provider not in {"claude", "gemini", "mlx_local"}:
        msg = f"unknown provider: {provider!r} (expected: claude, gemini, mlx_local)"
        raise UserError(msg)

    p = make_provider(decision.provider, cfg.cli.providers)
    opts = ChatOpts(stream=not no_stream, max_tokens=max_tokens, temperature=temperature)
    messages = [Message(role="user", content=text)]

    with telemetry.Span(cfg.cli.telemetry, command="chat") as span:
        span.set(
            provider=decision.provider,
            route_rule=decision.rule,
            intent="chat",
            prompt=text,
        )
        try:
            for chunk in p.chat(messages, opts):
                sys.stdout.write(chunk)
                sys.stdout.flush()
        except Exception as exc:
            msg = f"{decision.provider} call failed: {type(exc).__name__}: {exc}"
            raise ProviderError(msg, fix="run `sanctum doctor` to probe provider health") from exc

    sys.stdout.write("\n")
    sys.stdout.flush()


def _resolve_prompt(prompt: str | None, file: Path | None) -> str:
    if prompt and file:
        msg = "pass either a positional prompt or --file, not both"
        raise UserError(msg)
    if file is not None:
        try:
            return file.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"cannot read --file {file}: {exc}"
            raise UserError(msg, fix="check the path and permissions") from exc
    if prompt is not None:
        return prompt
    if sys.stdin.isatty():
        msg = "no prompt provided"
        raise UserError(msg, fix='pass a prompt: sanctum chat "..."  (or pipe text on stdin)')
    return sys.stdin.read()


def _validate_provider(name: str | None) -> ProviderName | None:
    if name is None:
        return None
    if name not in _VALID:
        msg = f"unknown provider: {name!r} (expected: claude, gemini, mlx_local)"
        raise UserError(msg)
    return cast("ProviderName", name)
