"""Top-level Typer app + global error trap.

Every subcommand is registered here. Uncaught exceptions are caught at
this boundary and translated to the right exit code (per
``sanctum_cli.errors.ExitCode``); structured ``SanctumError`` instances
print a one-liner with their suggested fix, generic exceptions get a
backtrace under ``--traceback``.
"""

from __future__ import annotations

import sys
from pathlib import Path  # noqa: TC003 - Typer evaluates the annotation at decoration time
from typing import Annotated

import typer
from rich.console import Console

from sanctum_cli import __version__
from sanctum_cli.commands import chat as chat_cmd
from sanctum_cli.commands import config_cmd, doctor, status
from sanctum_cli.errors import ExitCode, SanctumError

app = typer.Typer(
    name="sanctum",
    help="Unified terminal binary for Sanctum hosts — router, wizard, doctor.",
    no_args_is_help=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

config_app = typer.Typer(help="Configuration commands.")
app.add_typer(config_app, name="config")


@config_app.command("validate", help="Schema-check ~/.sanctum/instance.yaml against the cli schema.")
def config_validate_top(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        config_cmd.validate_command(json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc

err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sanctum {__version__}")
        raise typer.Exit(code=ExitCode.OK)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
    traceback: Annotated[
        bool,
        typer.Option("--traceback", help="Print full traceback on uncaught exceptions."),
    ] = False,
) -> None:
    """Sanctum CLI entry point. With no subcommand, prints status one-liner."""
    ctx.ensure_object(dict)
    ctx.obj["traceback"] = traceback

    if ctx.invoked_subcommand is None:
        # Default: brevity-gated status one-liner
        try:
            status.status_command(json_output=False, oneline=True)
        except SanctumError as exc:
            _report(exc)
            raise typer.Exit(code=int(exc.exit_code)) from exc


# Register status as a top-level command too (`sanctum status [--json]`)
@app.command("status", help="Health snapshot — backup age, providers, disk, telemetry summary.")
def status_top(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON instead of human-readable.")] = False,
    oneline: Annotated[bool, typer.Option("--oneline", help="Force one-line summary even if errors exist.")] = False,
) -> None:
    try:
        status.status_command(json_output=json_output, oneline=oneline)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@app.command("chat", help="Send a prompt to the router-chosen provider; stream the response.")
def chat_top(
    prompt: Annotated[
        str | None, typer.Argument(help="Prompt text. Omit to read from stdin.")
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider", "-p", help="Force provider: claude | gemini | mlx_local."
        ),
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Read prompt from a file.")
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
    try:
        chat_cmd.chat_command(
            prompt=prompt,
            provider=provider,
            file=file,
            no_stream=no_stream,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@app.command("doctor", help="Health probes — LaunchAgents, providers, backup repos.")
def doctor_top(
    full: Annotated[
        bool, typer.Option("--full", help="Always print full per-row detail.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON (full report regardless of --full).")
    ] = False,
) -> None:
    try:
        doctor.doctor_command(full=full, json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


def _report(exc: SanctumError) -> None:
    """Pretty-print a SanctumError to stderr with optional fix suggestion."""
    err_console.print(f"[bold red]error:[/] {exc.message}")
    if exc.fix:
        err_console.print(f"[dim]fix:[/] {exc.fix}")


def _excepthook(_exc_type: type[BaseException], exc: BaseException, _tb: object) -> None:  # pragma: no cover
    """Catch-all for non-Sanctum exceptions; map to LOCAL_ERROR exit code."""
    err_console.print(f"[bold red]internal error:[/] {exc!r}")
    err_console.print("[dim]rerun with --traceback for details[/]")
    sys.exit(int(ExitCode.LOCAL_ERROR))


sys.excepthook = _excepthook
