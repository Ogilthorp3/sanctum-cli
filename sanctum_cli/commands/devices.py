"""``sanctum devices`` — list devices in the haushold.

Reads from the canonical source-of-truth (devices.yaml in the screen-time
module). Auto-discovered devices that haven't been assigned to a category
show up under "unassigned" with their OUI-derived guess.

Read-only by design — assigning a device is via ``sanctum screen-time
assign`` (Phase 2 of Family Pass) or by editing the YAML.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table
from rich.text import Text

console = Console()


def _load_devices() -> dict[str, Any] | None:
    """Find devices.yaml in the canonical screen-time module location."""
    override = os.environ.get("SANCTUM_DEVICES_FILE")
    candidates = (
        [Path(override).expanduser()]
        if override
        else [
            Path.home() / ".sanctum/screen-time/devices.yaml",
            Path.home() / "Projects/sanctum-screen-time/devices.yaml",
        ]
    )
    for p in candidates:
        if p.is_file():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                return None
            return data if isinstance(data, dict) else None
    return None


def devices_command() -> None:
    """Show the haushold's device inventory."""
    data = _load_devices()
    if data is None:
        console.print("[yellow]No devices.yaml found.[/]")
        console.print("Expected at one of:")
        for p in (
            Path.home() / ".sanctum/screen-time/devices.yaml",
            Path.home() / "Projects/sanctum-screen-time/devices.yaml",
        ):
            console.print(f"  {p}")
        raise typer.Exit(code=2)

    family = data.get("family", {})
    if family:
        table = Table(title="Family devices", show_header=True, header_style="bold cyan")
        table.add_column("Person")
        table.add_column("Device")
        table.add_column("MAC")
        for person_id, person in family.items():
            # Real schema: personal_devices (not "devices"); member key is the name.
            devices = person.get("personal_devices", [])
            if not devices:
                continue
            for d in devices:
                table.add_row(
                    Text(str(person.get("name", person_id))),
                    Text(str(d.get("name", "?"))),
                    Text(str(d.get("mac", "?"))),
                )
        console.print(table)
        console.print()

    # Real schema: shared_devices is a {key: {name, mac}} mapping, not a list.
    shared = data.get("shared_devices") or {}
    if shared:
        table = Table(title="Shared devices", show_header=True, header_style="bold cyan")
        table.add_column("Key")
        table.add_column("Name")
        table.add_column("MAC")
        for key, d in shared.items():
            table.add_row(
                Text(str(key)),
                Text(str(d.get("name", "?"))),
                Text(str(d.get("mac", "?"))),
            )
        console.print(table)
        console.print()

    unassigned = data.get("unassigned", [])
    if unassigned:
        table = Table(title="Unassigned (auto-discovered)", show_header=True, header_style="bold yellow")
        table.add_column("MAC")
        table.add_column("Guess")
        table.add_column("First seen")
        for d in unassigned:
            table.add_row(
                str(d.get("mac", "?")),
                str(d.get("oui_guess", "?")),
                str(d.get("first_seen", "?")),
            )
        console.print(table)
