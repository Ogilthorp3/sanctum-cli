"""``sanctum hive`` — opt-in Sovereign Hive Pulse & public Hive statistics."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sanctum_cli import hive_pulse

console = Console()
app = typer.Typer(
    name="hive",
    help="Opt-in anonymous Sanctum Hive counter (zero IP, zero PII).",
    no_args_is_help=True,
)


def _print_seal_panel(seal: dict[str, Any], *, svg_path: str = "") -> None:
    tier = str(seal.get("tier") or "sovereign")
    title = str(seal.get("title") or "Sovereign Node")
    motto = str(seal.get("motto") or "Local silicon, zero cloud leash.")
    claim_code = str(seal.get("claim_code") or "")
    minted = str(seal.get("minted_at") or "")[:10]
    pub_hash = str(seal.get("public_hash") or "")[:16].upper()
    verify_url = str(seal.get("verify_url") or "")

    border = "yellow" if tier in ("seed-1", "founding-circle", "seed-0") else "cyan"
    icon = "◆" if tier in ("seed-1", "seed-0") else "◇"
    lines = [
        f"[bold yellow]{icon} {title}[/]" if border == "yellow" else f"[bold cyan]{icon} {title}[/]",
        f"[dim]{motto}[/]",
        "",
        f"Claim Code:  [bold]{claim_code}[/]  [dim](keep private unless claiming publicly)[/]",
        f"Minted:      {minted}   ·   Seal Hash: {pub_hash}",
    ]
    if svg_path:
        lines.append(f"SVG Crest:   [cyan]{svg_path}[/]")
    if verify_url:
        lines.append(f"Verify:      [dim]{verify_url}[/]")
    lines.append("[dim]View anytime with `sanctum hive seal` · Gold crest auto-unlocked in `sanctum ui`.[/]")
    console.print(Panel.fit("\n".join(lines), title="Sanctum Sovereign Seal", border_style=border))


@app.command("join", help="Opt into the anonymous Sanctum Hive counter and mint your Sovereign Seal.")
def hive_join(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    res = hive_pulse.join_hive(event="join")
    if json_output:
        print(json.dumps(res, indent=2))
        return

    payload = res.get("payload") or {}
    if res.get("ok"):
        console.print(
            f"[green]✓[/] Joined the Sanctum Hive "
            f"([bold]{res.get('hive_nodes', 1)}[/] Macs total · "
            f"[bold]{res.get('active_7d', 1)}[/] active this week)"
        )
        seal = res.get("seal")
        if isinstance(seal, dict) and seal.get("tier"):
            _print_seal_panel(seal, svg_path=str(res.get("seal_svg_path") or ""))
    else:
        console.print(
            f"[yellow]![/] Opt-in saved locally, but could not reach {res.get('endpoint')}: "
            f"{res.get('error', 'offline')} — will retry on your next `sanctum status`."
        )
    console.print(
        f"  [dim]Sent (zero PII): chip={payload.get('chip')}, ram={payload.get('memory_gb')}GB, "
        f"os={payload.get('os_version')}, country={payload.get('country')}, "
        f"cli={payload.get('cli_version')}, modules={payload.get('modules')}[/]"
    )
    console.print("  [dim]Leave and erase anytime: `sanctum hive leave`[/]")


@app.command("seal", help="Display your local cryptographic Sovereign Seal, export SVG crest, or verify any claim code.")
def hive_seal(
    verify: Annotated[
        str,
        typer.Option("--verify", help="Verify a Sovereign Seal claim code (e.g. SEED1-XXXX-XXXX) online with zero PII."),
    ] = "",
    open_svg: Annotated[
        bool,
        typer.Option("--open", help="Open your locally rendered SVG Sovereign Crest."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    if verify.strip():
        try:
            res = hive_pulse.verify_seal_remote(verify.strip())
        except Exception as exc:
            if json_output:
                print(json.dumps({"ok": False, "valid": False, "error": str(exc)}, indent=2))
                raise typer.Exit(code=1) from exc
            console.print(f"[red]Could not verify seal:[/] {exc}")
            raise typer.Exit(code=1) from exc

        if json_output:
            print(json.dumps(res, indent=2))
            return
        if res.get("valid"):
            console.print(
                Panel.fit(
                    f"[bold green]✓ AUTHENTIC SOVEREIGN SEAL[/]\n"
                    f"Seat:     [bold yellow]{res.get('title')}[/]\n"
                    f"Hardware: {res.get('chip')} · {res.get('memory_gb')} GB · {res.get('country')}\n"
                    f"Minted:   {res.get('minted_date')} · Hash: {str(res.get('public_hash') or '').upper()}\n"
                    f"[dim]Verified zero-PII against hive.sanctum.run[/]",
                    border_style="yellow",
                )
            )
            return
        console.print(f"[red]✗ Seal code not recognized:[/] {verify.strip().upper()}")
        raise typer.Exit(code=1)

    seal = hive_pulse.load_seal()
    if not seal:
        if json_output:
            print(json.dumps({"ok": False, "error": "no_local_seal"}, indent=2))
            raise typer.Exit(code=1)
        console.print("[yellow]No local Sovereign Seal yet.[/] Run [bold]sanctum hive join[/] to mint yours.")
        raise typer.Exit(code=1)

    svg_path = hive_pulse.seal_svg_path()
    if not svg_path.is_file():
        _, svg_path = hive_pulse.save_seal(seal)

    if json_output:
        print(json.dumps({**seal, "svg_path": str(svg_path)}, indent=2))
        return

    _print_seal_panel(seal, svg_path=str(svg_path))
    if open_svg and shutil.which("open"):
        subprocess.run(["open", str(svg_path)], check=False)


@app.command("leave", help="Opt out and erase your anonymous node from hive.sanctum.run.")
def hive_leave(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    res = hive_pulse.leave_hive()
    if json_output:
        print(json.dumps(res, indent=2))
        return
    if res.get("ok"):
        msg = "erased from hive.sanctum.run" if res.get("removed") else "not present on server"
        console.print(f"[green]✓[/] Opted out of Sanctum Hive Pulse ({msg}).")
    else:
        console.print(
            f"[yellow]![/] Opted out locally; remote delete failed ({res.get('error')})."
        )


@app.command("status", help="Show local Hive Pulse opt-in status and exact anonymous payload.")
def hive_status(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    state = hive_pulse.load_state()
    enabled = bool(state.get("enabled"))
    node_id = str(state.get("node_id") or "(not generated yet)")
    preview = hive_pulse.build_pulse_payload(
        node_id=node_id if len(node_id) >= 16 else "0" * 32,
        event="heartbeat",
        onboard_completed=bool(state.get("onboard_completed")),
    )
    out = {
        "enabled": enabled,
        "endpoint": state.get("endpoint") or hive_pulse.DEFAULT_HIVE_ENDPOINT,
        "node_id": node_id if enabled else None,
        "last_pulse_iso": state.get("last_pulse_iso"),
        "payload_preview": preview,
    }
    if json_output:
        print(json.dumps(out, indent=2))
        return

    badge = "[bold green]OPTED IN[/]" if enabled else "[dim]OPTED OUT[/]"
    console.print(
        Panel.fit(
            f"Hive Pulse: {badge}\n"
            f"Endpoint:   {out['endpoint']}\n"
            f"Last pulse: {out['last_pulse_iso'] or 'never'}\n"
            f"Payload:    chip={preview['chip']} · {preview['memory_gb']} GB · "
            f"{preview['country']} · v{preview['cli_version']} · modules={preview['modules']}\n"
            f"[dim]Zero IP · Zero hostname · Zero files. Toggle with `sanctum hive join|leave`.[/]",
            title="Sanctum Hive Pulse",
            border_style="cyan",
        )
    )


@app.command("stats", help="Show public aggregate Sanctum Hive statistics from hive.sanctum.run.")
def hive_stats(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        data = hive_pulse.fetch_hive_analytics()
    except Exception as exc:
        if json_output:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            raise typer.Exit(code=1) from exc
        console.print(f"[red]Could not fetch Hive stats:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        print(json.dumps(data, indent=2))
        return

    total_ram = int(data.get("total_ram_gb") or 0)
    ram_str = f"{total_ram / 1024:.2f} TB" if total_ram >= 1024 else f"{total_ram} GB"

    t = Table(title="The Sanctum Hive (zero-PII aggregate)", show_header=True, header_style="bold")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    t.add_row("Total Hive Macs", str(data.get("total_nodes", 0)))
    t.add_row("Completed Onboard", str(data.get("onboard_completed", 0)))
    t.add_row("Active (24h / 7d / 30d)", f"{data.get('active_24h', 0)} / {data.get('active_7d', 0)} / {data.get('active_30d', 0)}")
    t.add_row("Collective Silicon RAM", ram_str)
    t.add_row("Version checks (24h / 7d)", f"{data.get('version_checks_24h', 0)} / {data.get('version_checks_7d', 0)}")
    console.print(t)

    chips = data.get("by_chip") or {}
    countries = data.get("by_country") or {}
    if chips:
        console.print(
            "[dim]Chips:[/] " + ", ".join(f"{k} ({v})" for k, v in list(chips.items())[:6])
        )
    if countries:
        console.print(
            "[dim]Countries:[/] " + ", ".join(f"{k} ({v})" for k, v in list(countries.items())[:10])
        )
