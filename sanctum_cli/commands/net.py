from __future__ import annotations

import socket
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.markup import escape

from sanctum_cli.net import detect, playbooks, render, safety, system, verify
from sanctum_cli.net.types import Verdict

if TYPE_CHECKING:
    from sanctum_cli.net.detect import HttpProbe, Runner

console = Console()
net_app = typer.Typer(help="Network topology wizard and diagnostics.")

_SNAP_ROOT = Path.home() / ".sanctum" / "net-optimize"


def _build_runner() -> Runner:
    gw = detect.parse_default_gateway(system.real_runner(("route",)))
    key_path = Path.home() / ".ssh" / "firewalla_ed25519"
    fw_key = str(key_path) if key_path.exists() else None
    return system.make_real_runner(fw_gateway=gw, fw_key=fw_key)


def _build_http() -> HttpProbe:
    return system.real_http


def _firewalla_present() -> bool:
    try:
        socket.create_connection(("firewalla.local", 22), timeout=1).close()
        return True
    except OSError:
        return False


@net_app.command("check", help="Audit network topology (read-only, makes no changes).")
def net_check() -> None:
    runner, http = _build_runner(), _build_http()
    rep = detect.detect(runner=runner, http=http, firewalla_present=_firewalla_present())
    console.print(f"[bold]NAT topology:[/] {escape(rep.nat.value)}")
    console.print(
        f"[bold]ISP:[/] {escape(rep.isp)}   [bold]gateway:[/] {escape(rep.gateway_ip or '-')}"
    )
    console.print(escape(rep.reason))


@net_app.command("optimize", help="Guide you from double-NAT to single-NAT (opt-in, reversible).")
def net_optimize(
    assume_yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the presence prompt.")] = False,
    plan_only: Annotated[bool, typer.Option("--plan-only", help="Print the plan; skip verify loop.")] = False,
) -> None:
    runner, http = _build_runner(), _build_http()
    rep = detect.detect(runner=runner, http=http, firewalla_present=_firewalla_present())

    if not rep.applicable:
        if not rep.firewalla_present:
            console.print("[green]✓[/] No Firewalla here — nothing to optimize.")
        else:
            console.print(f"[green]✓[/] {escape(rep.reason)}")
        raise typer.Exit(code=0)

    pb = playbooks.BUILTINS.get(rep.isp, playbooks.BUILTINS["generic"])
    console.print(render.render_plan(rep, pb), markup=False)

    if plan_only:
        raise typer.Exit(code=0)

    # Capture the rollback baseline only on the real flow, before the user acts.
    safety.snapshot(rep, root=_SNAP_ROOT)
    if not assume_yes:
        console.print("\n[bold]This briefly drops your internet.[/] Are you at the box, not remote?")
        if not typer.confirm("Proceed?"):
            console.print("No changes made.")
            raise typer.Exit(code=0)

    console.print("\nWhen you've done the steps above, I'll verify. Press Enter to check…")
    typer.prompt("", default="", show_default=False)
    v, reason = verify.verify(runner=runner)
    if v is Verdict.VERIFIED:
        console.print(f"[green]✓ {escape(reason)}[/]")
    elif v is Verdict.APIPA_ROLLBACK:
        console.print(f"[red]✗ {escape(reason)}[/]")
        console.print("Roll back:")
        for line in pb.rollback:
            console.print(f"  ↩ {escape(line)}")
    else:
        console.print(f"[yellow]{escape(reason)}[/]")
