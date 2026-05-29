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
from sanctum_cli.commands import bridge as bridge_cmd
from sanctum_cli.commands import chat as chat_cmd
from sanctum_cli.commands import cloud as cloud_cmd
from sanctum_cli.commands import code as code_cmd
from sanctum_cli.commands import config_cmd, doctor, status
from sanctum_cli.commands import keychain_cmd as keychain_command
from sanctum_cli.commands import devices as devices_cmd
from sanctum_cli.commands import keys_backup as keys_backup_cmd
from sanctum_cli.commands import logs as logs_cmd
from sanctum_cli.commands import onboard as onboard_cmd
from sanctum_cli.commands import proxy as proxy_cmd
from sanctum_cli.commands import schedule as schedule_cmd
from sanctum_cli.commands import self_test as self_test_cmd
from sanctum_cli.commands import uninstall as uninstall_cmd
from sanctum_cli.commands import update as update_cmd
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


@app.command(
    "self-test",
    help="Canonical health probes — one command to verify the install is still good.",
)
def self_test_top(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of the panel.")
    ] = False,
    only: Annotated[
        str | None,
        typer.Option("--only", help="Substring filter: run only matching probes."),
    ] = None,
) -> None:
    self_test_cmd.self_test_command(json_output=json_output, only=only)


@app.command("update", help="brew upgrade sanctum-cli + run self-test as the gate.")
def update_top(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    skip_self_test: Annotated[bool, typer.Option("--skip-self-test")] = False,
) -> None:
    update_cmd.update_command(dry_run=dry_run, skip_self_test=skip_self_test)


@app.command("uninstall", help="Remove sanctum from this machine. Preserves data by default.")
def uninstall_top(
    purge: Annotated[bool, typer.Option("--purge")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    uninstall_cmd.uninstall_command(purge=purge, yes=yes, dry_run=dry_run)


@app.command("logs", help="Tail the log file for a sanctum service.")
def logs_top(
    service: Annotated[
        str, typer.Argument(help="Service name. Use 'list' to see what's known.")
    ],
    follow: Annotated[bool, typer.Option("--follow/--once", "-f")] = True,
    lines: Annotated[int, typer.Option("--lines", "-n")] = 50,
    list_services: Annotated[bool, typer.Option("--list")] = False,
) -> None:
    logs_cmd.logs_command(service=service, follow=follow, lines=lines, list_services=list_services)


@app.command("devices", help="List the haushold device inventory.")
def devices_top() -> None:
    devices_cmd.devices_command()


@app.command("schedule", help="Show the haushold curfew schedule.")
def schedule_top() -> None:
    schedule_cmd.schedule_command()


keys_app = typer.Typer(help="Keychain-backed credential helpers.")
app.add_typer(keys_app, name="keys")


@keys_app.command("backup", help="Export sanctum Keychain entries to an encrypted bundle.")
def keys_backup_top(
    out: Annotated[Path, typer.Argument(help="Where to write the encrypted bundle.")],
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    keys_backup_cmd.keys_backup_command(out=out, yes=yes)


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


@backup_app.command(
    "run",
    help="Run a backup. With --recipe, drives restic from a recipe; otherwise runs the legacy script.",
)
def backup_run_top(
    recipe: Annotated[
        str | None,
        typer.Option(
            "--recipe",
            "-r",
            help="Recipe name (family | operator | code | <user-defined>).",
        ),
    ] = None,
    script: Annotated[
        Path | None,
        typer.Option("--script", help="Path to backup script.", exists=True, dir_okay=False),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be backed up; write nothing."),
    ] = False,
) -> None:
    try:
        backup_cmd.backup_run(recipe=recipe, script=script, dry_run=dry_run)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@backup_app.command("recipes", help="List available backup recipes (built-in + user-defined).")
def backup_recipes_top(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        backup_cmd.backup_recipes(json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@backup_app.command(
    "estimate",
    help="Estimate raw size for a recipe before running it. Compares against R2 free tier.",
)
def backup_estimate_top(
    recipe: Annotated[str, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        backup_cmd.backup_estimate(recipe=recipe, json_output=json_output)
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


@cloud_app.command(
    "setup",
    help="Guided wizard to wire a backup target (r2 | b2 | gdrive | github Tier 0).",
)
def cloud_setup_top(
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help="Backend: r2 (egress-free, recommended) | b2 | gdrive | github (Tier 0).",
        ),
    ] = "r2",
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


@app.command(
    "onboard",
    help="One-shot first-run: recipe → cloud setup → first backup → canary.",
)
def onboard_top(
    recipe: Annotated[
        str, typer.Option("--recipe", "-r", help="Recipe (family | operator | code).")
    ] = "family",
    backend: Annotated[
        str, typer.Option("--backend", help="Cloud backend (r2 | b2 | gdrive).")
    ] = "r2",
    no_open: Annotated[bool, typer.Option("--no-open")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    try:
        onboard_cmd.onboard_command(
            recipe=recipe, backend=backend, no_open=no_open, yes=yes
        )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


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


# ─── bridge subcommands ────────────────────────────────────────────

bridge_app = typer.Typer(
    help="Talk to the Sanctum Bridge gateway (CF Access + HMAC + SharePoint)."
)
app.add_typer(bridge_app, name="bridge")


@bridge_app.command("health", help="Liveness check on the bridge.")
def bridge_health_top(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        bridge_cmd.health_command(json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@bridge_app.command("whoami", help="Show effective bridge config (host + Keychain creds, redacted).")
def bridge_whoami_top() -> None:
    try:
        bridge_cmd.whoami_command()
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@bridge_app.command("manifest", help="List modules + actions exposed by the bridge.")
def bridge_manifest_top(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        bridge_cmd.manifest_command(json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@bridge_app.command("folder", help="Look up a SharePoint folder by tenant-relative path.")
def bridge_folder_top(
    path: Annotated[str, typer.Argument(help="e.g. Deals/Calder/Memos")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        bridge_cmd.folder_command(path=path, json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@bridge_app.command("children", help="List the children of a SharePoint folder (read).")
def bridge_children_top(
    path: Annotated[str, typer.Argument(help="e.g. Deals/Calder/Memos")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        bridge_cmd.children_command(path=path, json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@bridge_app.command("download", help="Download a SharePoint file (read), optionally extracting text.")
def bridge_download_top(
    path: Annotated[str, typer.Argument(help="Tenant-relative file path, e.g. Deals/Calder/memo.docx")],
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write decoded bytes to this path.", dir_okay=False),
    ] = None,
    extract_text: Annotated[
        bool,
        typer.Option(
            "--extract-text",
            help="Also return server-side plain text for .docx/.pdf/.xlsx.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        bridge_cmd.download_command(
            path=path,
            out=out,
            extract_text=extract_text,
            json_output=json_output,
        )
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@bridge_app.command(
    "doctor",
    help="Diagnose the bridge end-to-end: keychain, daemon, CF Access, rotator, allowlist.",
)
def bridge_doctor_top(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        bridge_cmd.doctor_command(json_output=json_output)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


@bridge_app.command("upload", help="Upload a file to a SharePoint folder via the bridge.")
def bridge_upload_top(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    folder: Annotated[str, typer.Argument(help="Tenant-relative path, e.g. Deals/Calder/Memos")],
    if_exists: Annotated[
        str,
        typer.Option(
            "--if-exists",
            help="version | overwrite | rename | fail (default: version).",
        ),
    ] = "version",
    doc_type: Annotated[
        str | None,
        typer.Option("--doc-type", help="Convenience for --metadata doc_type=<value>."),
    ] = None,
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", "-m", help="Metadata as key=value (repeatable)."),
    ] = None,
    no_create_folders: Annotated[
        bool,
        typer.Option(
            "--no-create-folders",
            help="Fail rather than create missing intermediate folders.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        bridge_cmd.upload_command(
            file=file,
            folder=folder,
            if_exists=if_exists,
            doc_type=doc_type,
            metadata=metadata,
            no_create_folders=no_create_folders,
            json_output=json_output,
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
