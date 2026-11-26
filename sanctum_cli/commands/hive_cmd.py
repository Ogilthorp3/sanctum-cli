"""``sanctum hive`` — opt-in Sovereign Hive Pulse & public Hive statistics."""

from __future__ import annotations

import json
from typing import Annotated

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


@app.command("join", help="Opt into the anonymous Sanctum Hive counter and send a pulse.")
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
