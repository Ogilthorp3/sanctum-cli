"""``sanctum module list`` and ``sanctum module status`` — read-only module commands.

``list``   — Rich table: module, version, description, last ship verdict (if a
             cached soak result exists, otherwise "-").

``status`` — Per-module detail: services, secrets (present/missing via
             ``keychain.exists``), probes, soak age (via ``classify_soak`` if
             the result file exists).

On unknown module name, prints the ``ManifestError`` message and exits with
code 2.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from sanctum_cli import keychain
from sanctum_cli.modules.manifest import ManifestError
from sanctum_cli.modules.registry import ModuleRegistry
from sanctum_cli.soak import SoakResult, classify_soak

module_app = typer.Typer(help="Inspect installed Sanctum modules.")

console = Console()


def _load_soak_result(module: str, result_path_template: str) -> SoakResult | None:
    """Load the soak result file for *module*, or return None if absent/corrupt."""
    result_path = Path(result_path_template.replace("{module}", module)).expanduser()
    if not result_path.is_file():
        return None
    try:
        return SoakResult.model_validate_json(result_path.read_text())
    except (ValueError, OSError):
        return None


def _soak_summary(module: str, result_path_template: str) -> str:
    """Return a human-readable soak age + status string, or '-' if not recorded."""
    result = _load_soak_result(module, result_path_template)
    if result is None:
        return "-"
    days, clean = classify_soak(result)
    status = "clean" if clean else "dirty"
    return f"{days:.1f}d {status}"


@module_app.command("list", help="List all installed modules with version and description.")
def list_modules() -> None:
    """Render a Rich table of all discovered modules."""
    registry = ModuleRegistry.discover()
    names = registry.names()

    t = Table(
        title="Sanctum modules",
        show_header=True,
        header_style="bold",
    )
    t.add_column("module", no_wrap=True)
    t.add_column("version", no_wrap=True)
    t.add_column("description")
    t.add_column("soak", no_wrap=True)

    for name in names:
        m = registry.get(name)
        soak_col = _soak_summary(name, m.soak.result_path)
        t.add_row(name, m.version, m.description, soak_col)

    console.print(t)


@module_app.command("status", help="Show detail for one installed module.")
def module_status(
    name: Annotated[str, typer.Argument(help="Module name, e.g. backup.")],
) -> None:
    """Show services, secrets, probes, and soak age for *name*."""
    registry = ModuleRegistry.discover()
    try:
        m = registry.get(name)
    except ManifestError as exc:
        console.print(str(exc))
        raise typer.Exit(2) from exc

    console.print(f"[bold]Module:[/] {m.module}  [dim]v{m.version}[/]")
    console.print(f"[dim]{m.description}[/]")
    console.print()

    # ── services ──────────────────────────────────────────────────────
    if m.services:
        console.print("[bold]Services[/]")
        for svc in m.services:
            keepalive_note = " [keepalive]" if svc.keepalive else ""
            console.print(f"  {svc.label}  ({svc.kind.value}){keepalive_note}")
    else:
        console.print("[bold]Services:[/] [dim]none declared[/]")
    console.print()

    # ── secrets ───────────────────────────────────────────────────────
    if m.secrets:
        console.print("[bold]Secrets[/]")
        for sec in m.secrets:
            present = keychain.exists(sec.account, sec.service)
            mark = Text("present", style="green") if present else Text("missing", style="red")
            req_note = "" if sec.required else " [dim](optional)[/dim]"
            console.print(Text.assemble(
                Text(f"  {sec.service}  "),
                mark,
                Text(req_note),
            ))
    else:
        console.print("[bold]Secrets:[/] [dim]none declared[/]")
    console.print()

    # ── probes ────────────────────────────────────────────────────────
    if m.probes:
        console.print("[bold]Probes[/]")
        for probe in m.probes:
            console.print(f"  {probe}")
    else:
        console.print("[bold]Probes:[/] [dim]none declared[/]")
    console.print()

    # ── soak ──────────────────────────────────────────────────────────
    soak_summary = _soak_summary(m.module, m.soak.result_path)
    console.print(f"[bold]Soak:[/] {soak_summary}  [dim](target: {m.soak.min_days}d)[/]")
    console.print(f"[dim]docs: {m.docs}[/]")
    console.print(f"[dim]demo: {m.demo}[/]")
