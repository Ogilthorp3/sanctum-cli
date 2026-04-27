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
from sanctum_cli.commands import agent as agent_cmd
from sanctum_cli.commands import backup as backup_cmd
from sanctum_cli.commands import chat as chat_cmd
from sanctum_cli.commands import cloud as cloud_cmd
from sanctum_cli.commands import code as code_cmd
from sanctum_cli.commands import config_cmd, doctor, status
from sanctum_cli.commands import keychain_cmd as keychain_command
from sanctum_cli.commands import proxy as proxy_cmd
from sanctum_cli.commands import vision as vision_cmd
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


@app.command("code", help="Forced Claude routing — coding-oriented prompt.")
def code_top(
    prompt: Annotated[
        str | None, typer.Argument(help="Prompt text. Omit to read from stdin.")
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Read prompt from a file.")
    ] = None,
    no_stream: Annotated[
        bool, typer.Option("--no-stream", help="Wait for full response before printing.")
    ] = False,
    max_tokens: Annotated[
        int | None, typer.Option("--max-tokens", "-t", help="Cap response length.", min=1)
    ] = None,
    temperature: Annotated[
        float | None,
        typer.Option("--temperature", help="Sampling temperature 0.0..2.0.", min=0.0, max=2.0),
    ] = None,
) -> None:
    try:
        code_cmd.code_command(
            prompt=prompt,
            file=file,
            no_stream=no_stream,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


# ─── backup subcommands ─────────────────────────────────────────────

backup_app = typer.Typer(help="Backup commands — run, list snapshots, verify, restore.")
app.add_typer(backup_app, name="backup")


@backup_app.command("run", help="Run the configured sanctum-backup.sh script.")
def backup_run_top(
    script: Annotated[
        Path | None,
        typer.Option(
            "--script",
            help="Path to backup script.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    try:
        backup_cmd.backup_run(script=script)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@backup_app.command("snapshots", help="List restic snapshots from configured repos.")
def backup_snapshots_top(
    repo: Annotated[
        str, typer.Option("--repo", help="primary | secondary | all.")
    ] = "all",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        backup_cmd.backup_snapshots(repo=repo, json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@backup_app.command("verify", help="restic check on configured repos.")
def backup_verify_top(
    repo: Annotated[
        str, typer.Option("--repo", help="primary | secondary | all.")
    ] = "all",
) -> None:
    try:
        backup_cmd.backup_verify(repo=repo)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@backup_app.command("restore", help="Restore a snapshot to a target directory.")
def backup_restore_top(
    snapshot: Annotated[str, typer.Argument(help="Snapshot id (short or full).")],
    target: Annotated[Path, typer.Argument(help="Directory to restore into.")],
    repo: Annotated[
        str, typer.Option("--repo", help="primary | secondary.")
    ] = "primary",
) -> None:
    try:
        backup_cmd.backup_restore(snapshot=snapshot, target=target, repo=repo)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


# ─── cloud subcommands ──────────────────────────────────────────────

cloud_app = typer.Typer(help="Cloud-backup configuration wizards.")
app.add_typer(cloud_app, name="cloud")


@cloud_app.command("setup", help="Guided wizard to wire a cloud backup target (b2 | gdrive).")
def cloud_setup_top(
    backend: Annotated[
        str, typer.Option("--backend", help="Backend: b2 (recommended) | gdrive.")
    ] = "b2",
    no_open: Annotated[bool, typer.Option("--no-open", help="Don't auto-open browser tabs.")] = False,
    no_persist: Annotated[
        bool,
        typer.Option("--no-persist", help="Print YAML instead of editing instance.yaml."),
    ] = False,
) -> None:
    try:
        cloud_cmd.cloud_setup_command(backend=backend, no_open=no_open, no_persist=no_persist)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


# ─── vision (multimodal Gemini) ─────────────────────────────────────


@app.command("vision", help="Send an image / video to Gemini with a prompt.")
def vision_top(
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
    try:
        vision_cmd.vision_command(
            file=file,
            prompt=prompt,
            no_stream=no_stream,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


# ─── agent subcommands ─────────────────────────────────────────────

agent_app = typer.Typer(help="LaunchAgent management for com.sanctum.* labels.")
app.add_typer(agent_app, name="agent")


@agent_app.command("list", help="List loaded com.sanctum.* LaunchAgents.")
def agent_list_top(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        agent_cmd.agent_list(json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@agent_app.command("status", help="Status for one LaunchAgent.")
def agent_status_top(
    label: Annotated[str, typer.Argument(help="Label, e.g. com.sanctum.proxy.")],
) -> None:
    try:
        agent_cmd.agent_status(label)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@agent_app.command("start", help="Bootstrap (load) a LaunchAgent.")
def agent_start_top(label: Annotated[str, typer.Argument()]) -> None:
    try:
        agent_cmd.agent_start(label)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@agent_app.command("stop", help="Bootout (unload) a LaunchAgent.")
def agent_stop_top(label: Annotated[str, typer.Argument()]) -> None:
    try:
        agent_cmd.agent_stop(label)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@agent_app.command("restart", help="Bootout + bootstrap a LaunchAgent.")
def agent_restart_top(label: Annotated[str, typer.Argument()]) -> None:
    try:
        agent_cmd.agent_restart(label)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@agent_app.command("logs", help="Tail StandardOutPath/ErrorPath from the plist.")
def agent_logs_top(
    label: Annotated[str, typer.Argument()],
    follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", min=0, max=10_000)] = 50,
) -> None:
    try:
        agent_cmd.agent_logs(label, follow=follow, lines=lines)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


# ─── proxy subcommands ─────────────────────────────────────────────

proxy_app = typer.Typer(help="Manage local provider proxies (claude-cli-proxy / sanctum-server / lmstudio).")
app.add_typer(proxy_app, name="proxy")


@proxy_app.command("status", help="LaunchAgent + HTTP /v1/models probe.")
def proxy_status_top(
    target: Annotated[str, typer.Argument()] = "all",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        proxy_cmd.proxy_status(target=target, json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@proxy_app.command("restart", help="Restart one proxy LaunchAgent.")
def proxy_restart_top(target: Annotated[str, typer.Argument()]) -> None:
    try:
        proxy_cmd.proxy_restart(target=target)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@proxy_app.command("logs", help="Tail proxy logs.")
def proxy_logs_top(
    target: Annotated[str, typer.Argument()],
    follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", min=0)] = 50,
) -> None:
    try:
        proxy_cmd.proxy_logs(target=target, follow=follow, lines=lines)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


# ─── keychain subcommands ──────────────────────────────────────────

keychain_app = typer.Typer(help="Inspect and rotate sanctum-managed Keychain entries.")
app.add_typer(keychain_app, name="keychain")


@keychain_app.command("list", help="List managed entries (values never printed).")
def keychain_list_top(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        keychain_command.keychain_list(json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@keychain_app.command("test", help="Read every managed entry to confirm Keychain access.")
def keychain_test_top() -> None:
    try:
        keychain_command.keychain_test()
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@keychain_app.command("rotate", help="Replace a Keychain entry with a fresh value.")
def keychain_rotate_top(
    service: Annotated[str, typer.Argument()],
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Account (default: sanctum).")
    ] = None,
    value: Annotated[
        str | None,
        typer.Option("--value", help="Provide value. Omit to auto-generate 64 hex chars."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    try:
        keychain_command.keychain_rotate(
            service=service, account=account, new_value=value, yes=yes
        )
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
