"""``sanctum service-user`` — hive service principal (wave-1) install + check.

Subcommands:
  status   print wave-1 ownership / health
  check    same as status; exit 1 on failure (CI / self-test)
  install  run ~/.sanctum/scripts/service-user/install-on-new-hub.sh (sudo)
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from sanctum_cli import service_user as su
from sanctum_cli.errors import ConfigError, LocalError

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    help="Hive service principal — wave-1 LaunchDaemons under user `sanctum`.",
    no_args_is_help=True,
)


def _print_report(report: su.Wave1Report) -> None:
    if not report.applicable:
        console.print(
            f"[dim]n/a[/] service principal not expected on this install "
            f"({report.reason})"
        )
        return
    table = Table(title="Service user wave-1", show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail")
    for item in report.items:
        if item.skip:
            mark = "[yellow]skip[/]"
        elif item.ok:
            mark = "[green]ok[/]"
        else:
            mark = "[red]FAIL[/]"
        table.add_row(item.name, mark, item.detail or "")
    console.print(table)
    if report.ok:
        console.print("[green]wave-1 healthy — control plane runs as sanctum[/]")
    else:
        console.print("[red]wave-1 incomplete[/]")
        console.print(
            "[dim]fix:[/] sudo sanctum service-user install  "
            f"(or {su.install_script_path()})"
        )


@app.command("status", help="Show wave-1 ownership and health (no sudo).")
def status_cmd() -> None:
    report = su.check_wave1()
    _print_report(report)


@app.command(
    "check",
    help="Like status; exit 1 if wave-1 is expected and unhealthy.",
)
def check_cmd() -> None:
    report = su.check_wave1()
    _print_report(report)
    if report.applicable and not report.ok:
        raise typer.Exit(code=1)


@app.command(
    "install",
    help="Create user sanctum + install wave-1 LaunchDaemons (prompts for admin).",
)
def install_cmd(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Materialize packaged plists into ~/.sanctum/launchdaemons; no sudo.",
        ),
    ] = False,
) -> None:
    if dry_run:
        dest = su.materialize_assets()
        console.print(
            f"[dim]dry-run:[/] packaged plists → {dest}\n"
            f"[dim]would run as root:[/] create user sanctum + bootstrap LaunchDaemons"
        )
        return
    console.print(
        "Installing hive service principal (user `sanctum` + wave-1 daemons).\n"
        "Self-contained — no pre-synced sanctum-config required.\n"
        "macOS will ask for your administrator password once.\n"
    )
    try:
        rc = su.run_install(dry_run=False)
    except FileNotFoundError as exc:
        raise ConfigError(str(exc)) from exc
    except OSError as exc:
        raise LocalError(f"install failed to start: {exc}") from exc
    if rc != 0:
        raise LocalError(
            f"service-user install exited {rc}",
            fix="Read the output above; then: sanctum service-user status",
        )
    report = su.check_wave1()
    _print_report(report)
    # Plists+user may be OK while binaries are not yet on PATH — warn, don't always hard-fail
    if report.applicable and not report.ok:
        console.print(
            "[yellow]install finished; some checks still fail "
            "(binaries under ~/.sanctum/bin may be missing on a brand-new hub)[/]"
        )
        raise typer.Exit(code=1)
