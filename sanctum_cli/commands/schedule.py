"""``sanctum schedule`` — show the haushold's curfew schedule.

Read-only view. Mutations are still through the screen-time API endpoints
(``POST /screen/override`` for one-off extensions) — that path has its
own audit trail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import typer
import yaml
from rich.console import Console
from rich.table import Table

from sanctum_cli.haus import haus_required

console = Console()


def _load_devices() -> dict[str, Any] | None:
    candidates = [
        Path.home() / ".sanctum/screen-time/devices.yaml",
        Path.home() / "Projects/sanctum-screen-time/devices.yaml",
    ]
    for p in candidates:
        if p.is_file():
            try:
                return cast("dict[str, Any] | None", yaml.safe_load(p.read_text(encoding="utf-8")))
            except yaml.YAMLError:
                return None
    return None


def schedule_command() -> None:
    """Show the haushold's curfew schedule."""
    haus_required("screen-time")
    data = _load_devices()
    if data is None:
        console.print("[yellow]No devices.yaml found — no schedule to show.[/]")
        raise typer.Exit(code=2)

    family = data.get("family", {})
    if family:
        table = Table(title="Per-child curfew schedule", show_header=True, header_style="bold cyan")
        table.add_column("Person")
        table.add_column("Weekday curfew")
        table.add_column("Weekend curfew")
        table.add_column("Wake")
        for person_id, person in family.items():
            curfew = person.get("curfew", {})
            if isinstance(curfew, dict):
                weekday = curfew.get("weekday", "—")
                weekend = curfew.get("weekend", "—")
            else:
                weekday = weekend = str(curfew)
            wake = person.get("wake", "—")
            table.add_row(
                str(person.get("name", person_id)),
                str(weekday),
                str(weekend),
                str(wake),
            )
        console.print(table)
        console.print()

    shared = data.get("shared_devices", [])
    if any(d.get("hard_curfew") for d in shared):
        table = Table(
            title="Shared-device hard curfews", show_header=True, header_style="bold cyan"
        )
        table.add_column("Device")
        table.add_column("Hard curfew")
        for d in shared:
            hc = d.get("hard_curfew")
            if hc:
                table.add_row(str(d.get("name", d.get("key", "?"))), str(hc))
        console.print(table)
        console.print()

    console.print(
        "[dim]To extend a child's curfew once (e.g., for a school event):[/]\n"
        '  curl -X POST http://127.0.0.1:4077/screen/override -d \'{"target":"NAME","minutes":30}\''
    )
