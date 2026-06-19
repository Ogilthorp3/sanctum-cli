"""``sanctum init`` — create a minimal, valid ``instance.yaml``.

The first-run blocker (v0.9.0): every primary command calls ``config.load()``,
which hard-raises ``ConfigError`` when ``~/.sanctum/instance.yaml`` is absent —
so a stranger on a brand-new Mac hit a wall, with a fix that told them to
hand-write YAML. ``sanctum init`` writes the smallest file the loader accepts
(``instance.name`` + ``instance.slug``) so the box is usable in one command.

Behaviour:
  - Interactive: prompts for the instance name with a hostname-derived default,
    derives the slug from the chosen name.
  - ``--name`` sets the name without prompting; ``--yes`` accepts the default
    name for a fully non-interactive run (CI, scripted installs).
  - Idempotent: an existing file is left untouched (exit 0, a note) unless
    ``--force`` overwrites it.
  - Honours ``$SANCTUM_INSTANCE_FILE`` via :func:`config.instance_path`.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.prompt import Prompt

from sanctum_cli import config

console = Console()


def init_command(
    name: Annotated[
        str | None,
        typer.Option("--name", help="Instance name (skips the prompt). Slug is derived from it."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Accept the hostname-derived default; no prompts."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing instance.yaml."),
    ] = False,
) -> None:
    """Scaffold a minimal ``instance.yaml`` so a fresh machine can run the CLI."""
    target = config.instance_path()

    if target.exists() and not force:
        console.print(f"[green]✓[/] {target} already exists — nothing to do")
        console.print("[dim]re-run with --force to overwrite it[/]")
        return

    default_name, _ = config._default_identity()
    if name:
        chosen = name.strip() or default_name
    elif yes:
        chosen = default_name
    else:
        chosen = Prompt.ask("instance name", default=default_name).strip() or default_name

    written = config.scaffold_instance(target, name=chosen)
    # Load back through the strict validator so we only ever claim success on a
    # file the rest of the CLI can actually read (contract at the boundary).
    cfg = config.load(written)
    verb = "overwrote" if (force and target.exists()) else "wrote"
    console.print(
        f"[green]✓[/] {verb} {written}\n"
        f"  instance: [bold]{cfg.instance.name}[/] (slug={cfg.instance.slug})"
    )
    console.print("[dim]next: [cyan]sanctum onboard --recipe family[/] to wire a backup[/]")
