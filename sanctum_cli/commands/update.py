"""``sanctum update`` — pull the latest sanctum-cli via the Homebrew tap.

Wraps ``brew upgrade ogilthorp3/sanctum/sanctum-cli`` with a self-test
gate: refuses to call it "done" unless ``sanctum self-test`` passes
after the upgrade. The two-line equivalent for the operator, but the
self-test gate is the value-add (an upgrade that silently broke
something is the kind of thing the operator finds out at 3am).
"""

from __future__ import annotations

import subprocess
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()


def update_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would happen, don't do it.")
    ] = False,
    skip_self_test: Annotated[
        bool,
        typer.Option("--skip-self-test", help="Don't run self-test after upgrade."),
    ] = False,
) -> None:
    """Pull the latest sanctum-cli + run self-test as the gate."""
    console.print()
    console.print(Panel.fit("[bold]sanctum update[/]", border_style="cyan"))
    console.print()

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
            capture_output=True,
            text=True,
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
        console.print(
            "[dim]--skip-self-test: not running the gate. Run `sanctum self-test` manually.[/]"
        )
        raise typer.Exit(code=0)

    console.print()
    console.print("[bold]Step 3.[/] sanctum self-test (gate)")
    if dry_run:
        console.print("  [dim](dry-run, skipping)[/]")
        raise typer.Exit(code=0)

    # Invoke the self-test command in-process so the user sees the panel.
    from sanctum_cli.commands import self_test as st

    try:
        st.self_test_command(json_output=False, only=None)
    except typer.Exit as exc:
        if exc.exit_code == 0:
            console.print(
                Panel(
                    "[bold green]Update complete.[/] Self-test passed.",
                    border_style="green",
                )
            )
        else:
            console.print(
                Panel(
                    "[bold red]Update applied, but self-test failed.[/] Investigate before relying on the new version.",
                    border_style="red",
                )
            )
        raise
