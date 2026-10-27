"""``sanctum vault-armor`` — BFU/AFU encrypted APFS vault armor CLI wrapper.

Wraps the ``sanctum-vault-armor`` Rust binary (`~/.sanctum/bin/sanctum-vault-armor`)
so any Sanctum Haus can inspect, self-test, lock, unlock, or drill its 2-of-3
blinded Ed25519 TOPRF + Chaum-Pedersen DLEQ + ML-KEM-768 hybrid vault volume.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from sanctum_cli.errors import ExitCode, LocalError, SanctumError

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    help="BFU/AFU encrypted APFS vault armor (2-of-3 blinded TOPRF + ML-KEM-768).",
    no_args_is_help=True,
)


def binary_path() -> Path:
    override = os.environ.get("SANCTUM_VAULT_ARMOR_BIN")
    if override:
        return Path(override)
    return Path.home() / ".sanctum" / "bin" / "sanctum-vault-armor"


def _run_armor(args: list[str], timeout: int = 45) -> dict[str, object]:
    bin_p = binary_path()
    if not bin_p.exists():
        raise LocalError(
            f"sanctum-vault-armor binary not found at {bin_p}",
            fix="build and install from sanctum-rs: cargo build --release -p sanctum-vault-armor",
        )
    try:
        proc = subprocess.run(
            [str(bin_p), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalError(f"sanctum-vault-armor {' '.join(args)} timed out after {timeout}s") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise LocalError(
            f"sanctum-vault-armor {' '.join(args)} failed (rc={proc.returncode}): {detail}",
            fix="break-glass unlock via Apple diskutil: diskutil apfs unlockVolume SanctumVault",
        )
    out = (proc.stdout or "").strip()
    start = out.find("{")
    if start < 0:
        return {"raw": out}
    try:
        parsed = json.loads(out[start:])
    except json.JSONDecodeError:
        return {"raw": out}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _report_and_exit(exc: SanctumError) -> None:
    err_console.print(f"[bold red]error:[/] {exc.message}")
    if exc.fix:
        err_console.print(f"[dim]fix:[/] {exc.fix}")
    raise typer.Exit(code=int(exc.exit_code)) from exc


@app.command("status", help="Inspect APFS vault lock/mount state and optionally probe witnesses.")
def status_cmd(
    probe_witnesses: Annotated[
        bool,
        typer.Option("--probe-witnesses", help="Verify live DLEQ responses from configured witnesses."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit raw JSON.")] = False,
) -> None:
    args = ["status"]
    if probe_witnesses:
        args.append("--probe-witnesses")
    try:
        data = _run_armor(args)
    except SanctumError as exc:
        _report_and_exit(exc)
        return

    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return

    vol = data.get("volume") if isinstance(data.get("volume"), dict) else {}
    mounted = bool(vol.get("mounted")) if isinstance(vol, dict) else False
    locked = bool(vol.get("locked")) if isinstance(vol, dict) else True
    state_str = "[green]UNLOCKED & MOUNTED[/]" if mounted and not locked else "[yellow]LOCKED[/]"

    table = Table(title="Sanctum Vault Armor", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Vault ID", str(data.get("vault_id", "?")))
    table.add_row("Mode", str(data.get("mode", "?")))
    table.add_row("Threshold", str(data.get("threshold", "?")))
    table.add_row("Cipher Suite", str(data.get("post_quantum", "?")))
    table.add_row("State", state_str)
    if isinstance(vol, dict):
        table.add_row("Volume UUID", str(vol.get("volume_uuid", "?")))
        table.add_row("Mount Point", str(vol.get("mount_point") or "(unmounted)"))
    table.add_row("1Password Escrow", f"{data.get('op_breakglass_vault')}:{data.get('op_breakglass_item_id')}")
    probes = data.get("witness_probes")
    if isinstance(probes, list) and probes:
        for p in probes:
            if isinstance(p, dict):
                ok = bool(p.get("reachable_and_dleq_valid"))
                mark = "[green]DLEQ OK[/]" if ok else "[red]UNREACHABLE/DENIED[/]"
                table.add_row(f"Witness #{p.get('index')} ({p.get('label')})", mark)
    console.print(table)


@app.command("self-test", help="Run offline cryptographic & threshold DLEQ + ML-KEM-768 self-tests.")
def self_test_cmd(
    json_output: Annotated[bool, typer.Option("--json", help="Emit raw JSON.")] = False,
) -> None:
    try:
        data = _run_armor(["self-test"], timeout=20)
    except SanctumError as exc:
        _report_and_exit(exc)
        return
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return
    console.print("[green]PASS[/] sanctum-vault-armor cryptographic & threshold self-test passed.")


@app.command("unlock", help="Unlock SanctumVault via 2-of-3 blinded witness evaluation.")
def unlock_cmd(
    wait: Annotated[
        int,
        typer.Option("--wait", min=0, help="Max seconds to retry witnesses while network boots."),
    ] = 0,
    extra_share: Annotated[
        Path | None,
        typer.Option("--extra-share", help="Optional offline Share 3 JSON file path."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit raw JSON.")] = False,
) -> None:
    args = ["unlock", "--wait", str(wait)]
    if extra_share is not None:
        args.extend(["--extra-share", str(extra_share)])
    try:
        data = _run_armor(args, timeout=max(30, wait + 20))
    except SanctumError as exc:
        _report_and_exit(exc)
        return
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return
    console.print(
        f"[green]UNLOCKED[/] {data.get('volume_name')} at {data.get('mount_point')} "
        f"({data.get('last_action')})"
    )


@app.command("lock", help="Unmount and cryptographically lock SanctumVault.")
def lock_cmd(
    json_output: Annotated[bool, typer.Option("--json", help="Emit raw JSON.")] = False,
) -> None:
    try:
        data = _run_armor(["lock"], timeout=20)
    except SanctumError as exc:
        _report_and_exit(exc)
        return
    if json_output:
        typer.echo(json.dumps(data, indent=2))
        return
    console.print(f"[yellow]LOCKED[/] {data.get('volume_name')} ({data.get('volume_uuid')})")
    raise typer.Exit(code=int(ExitCode.OK))
