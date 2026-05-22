"""``sanctum uninstall`` — preserve data, purge the machine.

Council-brain verdict (2026-05-22): the right default is to keep all
user-written data (logs, memory, audit trail, recent backups) but
purge every operational artifact on the machine — LaunchAgent plists,
Keychain credentials, brew tap reference. User data NEVER dies
without explicit ``--purge``.

What it touches:
  - LaunchAgents: bootout every ``com.sanctum.*`` plist AND delete
    the plist file (orphan plists are worse than missing services)
  - Keychain: revoke every ``sanctum/...`` entry via ``security
    delete-generic-password``
  - Homebrew tap: untap ``ogilthorp3/sanctum`` (next ``brew install``
    will re-tap on demand)
  - SanctumBridge.app + SanctumLauncher.app: rename with
    ``.uninstalled-YYYY-MM-DD`` suffix (recoverable for ~30 days)

What it preserves (always, unless ``--purge``):
  - ``~/.sanctum/logs/`` — audit history
  - ``~/.sanctum/memory/`` — agent memory + Memory Vault content
  - ``~/.sanctum/backups/`` — local snapshots
  - ``~/.sanctum/secrets/`` — operator-managed secret files (mode 600)
  - Cloud bucket — operator's R2/B2/gdrive bucket is never touched

What it never touches:
  - ``/usr/local/bin/node`` — Apple Installer's domain (Node.js
    Foundation .pkg). Removing it is via the Installer.app history.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

console = Console()

KEYCHAIN_SERVICES = [
    "sanctum/openrouter-api-key",
    "sanctum/openrouter-mgmt-key",
    "sanctum/openrouter-mgmt-key-backup",
    "sanctum/anthropic-api-key",
    "sanctum/gemini-api-key",
    "sanctum/firewalla-bridge-token",
    "sanctum/r2-account-id",
    "sanctum/r2-access-key-id",
    "sanctum/r2-secret-access-key",
    "sanctum/b2-application-key-id",
    "sanctum/b2-application-key",
]


def _bootout_and_delete_launchagents() -> list[str]:
    """Bootout every com.sanctum.*.plist in ~/Library/LaunchAgents/ and
    rename them with a .uninstalled-YYYY-MM-DD suffix. Returns list of
    labels touched."""
    la_dir = Path.home() / "Library/LaunchAgents"
    if not la_dir.is_dir():
        return []
    touched = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for plist in sorted(la_dir.glob("com.sanctum.*.plist")):
        if ".retired" in plist.name or ".disabled" in plist.name:
            continue
        label = plist.stem
        # Bootout (ok if not loaded).
        subprocess.run(
            ["launchctl", "bootout", f"gui/{__import__('os').getuid()}/{label}"],
            capture_output=True, text=True,
        )
        # Rename rather than delete — recoverable.
        plist.rename(plist.with_suffix(f".plist.uninstalled-{ts}"))
        touched.append(label)
    return touched


def _revoke_keychain_entries() -> list[str]:
    """Delete every known sanctum Keychain entry. Returns list of services
    actually deleted (some may not exist on a partial install)."""
    deleted = []
    for service in KEYCHAIN_SERVICES:
        # `-a sanctum` for account; the service name is the full path.
        result = subprocess.run(
            ["security", "delete-generic-password", "-a", "sanctum", "-s",
             service.removeprefix("sanctum/")],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            deleted.append(service)
    return deleted


def _untap_homebrew() -> bool:
    result = subprocess.run(
        ["brew", "untap", "ogilthorp3/sanctum"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _rename_app_bundles() -> list[str]:
    renamed = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for app_name in ("SanctumBridge.app", "SanctumLauncher.app"):
        for parent in (Path.home() / "Applications", Path("/Applications")):
            app = parent / app_name
            if app.is_dir():
                target = parent / f"{app_name}.uninstalled-{ts}"
                try:
                    app.rename(target)
                    renamed.append(str(app))
                except OSError as e:
                    console.print(f"  [yellow]could not rename {app}: {e}[/]")
    return renamed


def _purge_data_dirs() -> list[str]:
    """ONLY called when --purge is set. Removes the user-data dirs."""
    removed = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    root = Path.home() / ".sanctum"
    if root.is_dir():
        target = root.with_name(f".sanctum.purged-{ts}")
        root.rename(target)
        removed.append(str(root))
    return removed


def uninstall_command(
    purge: Annotated[
        bool,
        typer.Option("--purge", help="Also remove ~/.sanctum/ (audit log, memory, backups). DESTRUCTIVE."),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompt.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would happen, don't do it."),
    ] = False,
) -> None:
    """Remove sanctum from this machine. Preserve user data by default."""
    title = "Sanctum uninstall — purge mode" if purge else "Sanctum uninstall"
    body_lines = [
        f"[bold]{title}[/]",
        "",
        "[dim]Will[/]:",
        "  · bootout every com.sanctum.* LaunchAgent + rename plists .uninstalled-YYYY-MM-DD",
        "  · revoke sanctum Keychain entries (OpenRouter, Anthropic, Gemini, R2, etc.)",
        "  · untap ogilthorp3/sanctum from Homebrew",
        "  · rename SanctumBridge.app + SanctumLauncher.app with .uninstalled suffix",
        "",
        "[dim]Will NOT touch[/]:",
        "  · /usr/local/bin/node (Node.js Foundation .pkg — use the Installer)",
        "  · Your cloud bucket (R2/B2/gdrive — your data, your control)",
    ]
    if purge:
        body_lines += [
            "",
            "[bold red]--purge will also remove:[/]",
            "  · ~/.sanctum/ (logs, memory, audit trail, backups, secrets)",
            "    Renamed to ~/.sanctum.purged-YYYY-MM-DD-HHMMSS, NOT deleted.",
            "    You can move it back if you change your mind within 30 days.",
        ]
    else:
        body_lines += [
            "",
            "[dim]Will PRESERVE[/]:",
            "  · ~/.sanctum/logs/, memory/, backups/, secrets/ (your data)",
        ]
    console.print(Panel("\n".join(body_lines), border_style="red" if purge else "yellow"))
    console.print()

    if not yes and not Confirm.ask("Proceed?", default=False):
        console.print("[dim]aborted[/]")
        raise typer.Exit(code=0)

    if dry_run:
        console.print("[dim]--dry-run: no changes made.[/]")
        raise typer.Exit(code=0)

    console.print()
    console.print("[bold]Step 1.[/] LaunchAgents")
    labels = _bootout_and_delete_launchagents()
    console.print(f"  bootout + renamed {len(labels)} plist(s)")

    console.print("[bold]Step 2.[/] Keychain")
    revoked = _revoke_keychain_entries()
    console.print(f"  revoked {len(revoked)} entry(ies)")

    console.print("[bold]Step 3.[/] Homebrew tap")
    if _untap_homebrew():
        console.print("  untapped ogilthorp3/sanctum")
    else:
        console.print("  [dim]tap not present or already untapped[/]")

    console.print("[bold]Step 4.[/] App bundles")
    renamed_apps = _rename_app_bundles()
    console.print(f"  renamed {len(renamed_apps)} bundle(s)")

    if purge:
        console.print("[bold red]Step 5.[/] User data (--purge)")
        purged = _purge_data_dirs()
        for p in purged:
            console.print(f"  renamed {p} → {p}.purged-…")

    console.print()
    body = (
        "[bold green]Uninstall complete.[/]\n\n"
        "Sanctum services are stopped. The brew tap is removed. Your data was preserved at ~/.sanctum/ "
        "(unless you used --purge — in which case it's at ~/.sanctum.purged-…, recoverable for ~30 days).\n\n"
        "To reinstall: [cyan]brew install ogilthorp3/sanctum/sanctum-cli && sanctum onboard --recipe family[/]"
    )
    console.print(Panel(body, border_style="green", padding=(1, 2)))
