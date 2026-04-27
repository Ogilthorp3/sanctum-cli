"""``sanctum config validate`` — schema-check ``instance.yaml``."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from sanctum_cli import config

console = Console()


def validate_command(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Validate the resolved instance config against the schema.

    Exit code 0 if valid, 5 (CONFIG_ERROR) with a precise pointer if not.
    The location of the file checked respects ``$SANCTUM_INSTANCE_FILE``.
    """
    target = config.instance_path()
    cfg = config.load(target)  # raises ConfigError if invalid

    if json_output:
        out = {"path": str(target), "ok": True, "instance": cfg.instance.model_dump()}
        typer.echo(json.dumps(out, indent=2))
        return

    console.print(f"[green]✓[/] {target} valid")
    console.print(f"  instance: [bold]{cfg.instance.name}[/] (slug={cfg.instance.slug})")
    console.print(f"  default provider: {cfg.cli.default_provider}")
    console.print(f"  routing rules:    {len(cfg.cli.routing.rules)}")
    console.print(f"  telemetry:        {'on' if cfg.cli.telemetry.enabled else 'off'}")
