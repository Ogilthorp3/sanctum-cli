"""``sanctum update`` — pull the latest sanctum-cli via the Homebrew tap.

Wraps ``brew upgrade ogilthorp3/sanctum/sanctum-cli`` with a self-test
gate: refuses to call it "done" unless ``sanctum self-test`` passes
after the upgrade. The two-line equivalent for the operator, but the
self-test gate is the value-add (an upgrade that silently broke
something is the kind of thing the operator finds out at 3am).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()


def _resolve_self_test_cmd() -> list[str]:
    """Resolve the post-upgrade ``sanctum self-test`` command on disk.

    Prefers ``shutil.which("sanctum")`` and Homebrew prefix bins before
    ``sys.executable`` so ``brew upgrade`` executes the newly symlinked
    version rather than an in-process pre-upgrade module or old Cellar venv.
    """
    which_bin = shutil.which("sanctum")
    if which_bin:
        return [which_bin, "self-test"]
    for candidate in (Path("/opt/homebrew/bin/sanctum"), Path("/usr/local/bin/sanctum")):
        if candidate.is_file():
            return [str(candidate), "self-test"]
    sibling = Path(sys.executable).with_name("sanctum")
    if sibling.is_file():
        return [str(sibling), "self-test"]
    return [sys.executable, "-m", "sanctum_cli", "self-test"]


def update_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would happen, don't do it.")
    ] = False,
    skip_self_test: Annotated[
        bool, typer.Option("--skip-self-test", help="Don't run self-test after upgrade."),
    ] = False,
) -> None:
    """Pull the latest sanctum-cli + run self-test as the gate."""
    console.print()
    console.print(Panel.fit("[bold]sanctum update[/]", border_style="cyan"))
    console.print()

    if not dry_run and not shutil.which("brew"):
        console.print("[red]Homebrew (brew) not found on PATH.[/]")
        raise typer.Exit(code=1)

    console.print("[bold]Step 1.[/] brew update")
    if dry_run:
        console.print("  [dim](dry-run, skipping)[/]")
    else:
        r = subprocess.run(["brew", "update"], capture_output=True, text=True)
        if r.returncode != 0:
            console.print(f"  [red]failed:[/] {r.stderr.strip()[:200]}")
            raise typer.Exit(code=1)
        console.print("  ok")

    console.print("[bold]Step 2.[/] brew upgrade ogilthorp3/sanctum/sanctum-cli")
    if dry_run:
        console.print("  [dim](dry-run, skipping)[/]")
    else:
        r = subprocess.run(
            ["brew", "upgrade", "ogilthorp3/sanctum/sanctum-cli"],
            capture_output=True, text=True,
        )
        # `brew upgrade` exits 0 even if already up-to-date; useful messages
        # land on stderr. Look for the "already installed" hint.
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            console.print(f"  [red]failed:[/] {out[-300:]}")
            raise typer.Exit(code=1)
        if "already installed" in out.lower():
            console.print("  [dim]already up-to-date[/]")
        else:
            console.print("  upgraded")

    if skip_self_test:
        console.print()
        console.print("[dim]--skip-self-test: not running the gate. Run `sanctum self-test` manually.[/]")
        raise typer.Exit(code=0)

    console.print()
    console.print("[bold]Step 3.[/] sanctum self-test (gate)")
    if dry_run:
        console.print("  [dim](dry-run, skipping)[/]")
        raise typer.Exit(code=0)

    cmd = _resolve_self_test_cmd()
    try:
        proc = subprocess.run(cmd, check=False)
    except OSError as exc:
        console.print(f"  [red]failed to launch self-test ({cmd[0]}):[/] {exc}")
        raise typer.Exit(code=1) from exc

    if proc.returncode == 0:
        console.print(Panel(
            "[bold green]Update complete.[/] Self-test passed.",
            border_style="green",
        ))
    else:
        console.print(Panel(
            "[bold red]Update applied, but self-test failed.[/] Investigate before relying on the new version.",
            border_style="red",
        ))
    raise typer.Exit(code=proc.returncode)
