"""``sanctum logs <service>`` — tail the right log file for a service.

Beta-tester-friendly: they don't need to know that R2D2's audit log is
``~/.sanctum/logs/r2d2-audit.jsonl`` or that proxyd's stderr lands at
``~/.openclaw/logs/proxyd.log``. They run ``sanctum logs r2d2`` and the
right tail-with-follow stream comes through.

Defaults to ``--follow`` and ``--lines 50``. ``--once`` for a snapshot.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()

LOG_MAP: dict[str, list[Path]] = {
    "r2d2": [Path.home() / ".sanctum/logs/r2d2-audit.jsonl"],
    "kitchen-loop": [Path.home() / ".sanctum/logs/kitchen-loop.log"],
    "proxyd": [Path.home() / ".openclaw/logs/proxyd.log"],
    "yoda": [Path.home() / ".openclaw/logs/sanctum-mlx.log"],
    "coder": [Path.home() / ".openclaw/logs/sanctum-mlx-coder.log"],
    "cathedral": [
        Path.home() / ".openclaw/logs/sanctum-mlx.log",
        Path.home() / ".openclaw/logs/sanctum-mlx-coder.log",
    ],
    "bridge": [Path.home() / ".openclaw/logs/sanctum-bridge.log"],
    "force-flow": [Path.home() / ".openclaw/logs/force-flow.log"],
    "watchdog": [Path.home() / ".openclaw/logs/watchdog-rust.log"],
    "agent-trace": [Path.home() / ".sanctum/logs/agent-trace.log"],
    "ram-sentinel": [Path.home() / ".sanctum/logs/ram-sentinel.log"],
    "launchd-health": [Path.home() / ".sanctum/logs/launchd-health.log"],
    "signal": [Path.home() / ".sanctum/logs/signal-health.log"],
    "backup": [Path.home() / ".sanctum/logs/backup.log"],
    "self-test": [Path.home() / ".sanctum/logs/self-test.log"],
    "log-rotate": [Path.home() / ".sanctum/logs/log-rotate.log"],
}


def logs_command(
    service: Annotated[
        str,
        typer.Argument(help="Service name. `sanctum logs --list` to see all known services."),
    ],
    follow: Annotated[
        bool,
        typer.Option("--follow/--once", "-f", help="Stream (default) or one-shot snapshot."),
    ] = True,
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", help="How many lines of history to show first."),
    ] = 50,
    list_services: Annotated[
        bool,
        typer.Option("--list", help="List known services + their log file paths."),
    ] = False,
) -> None:
    if list_services or service == "list":
        console.print("[bold]Known services:[/]")
        for name, log_paths in sorted(LOG_MAP.items()):
            for p in log_paths:
                marker = "✓" if p.is_file() else "·"
                console.print(f"  {marker}  {name:18s}  {p}")
        return

    paths: list[Path] | None = LOG_MAP.get(service.lower())
    if not paths:
        console.print(f"[red]unknown service:[/] {service}")
        console.print("Run `sanctum logs --list` to see what's known.")
        raise typer.Exit(code=2)

    # Filter to paths that actually exist.
    extant = [p for p in paths if p.is_file()]
    if not extant:
        console.print(f"[yellow]no log files exist yet for {service}.[/]")
        console.print("Expected locations:")
        for p in paths:
            console.print(f"  {p}")
        raise typer.Exit(code=2)

    args = ["tail", f"-n{lines}"]
    if follow:
        args.append("-f")
    args.extend(str(p) for p in extant)

    # exec replaces this process with tail so Ctrl-C is clean.
    with contextlib.suppress(KeyboardInterrupt):
        subprocess.run(args, check=False)
