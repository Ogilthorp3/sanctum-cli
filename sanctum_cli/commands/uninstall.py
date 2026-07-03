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

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

console = Console()

# (service, account). Names verified against the real write sites — the old
# list had drifted, so some keys SURVIVED 'uninstall': 'gemini-api-key' was
# never written (it's google-ai-api-key) and 'b2-application-key-id' should be
# 'b2-account-id'. 'firewalla-bridge-token' is a FILE (~/.sanctum/secrets), not
# a Keychain entry, so it's dropped here. The restic passphrase
# (sanctum-backup-key) is deliberately PRESERVED: uninstall keeps the cloud
# bucket + ~/.sanctum, and that passphrase is the only thing that decrypts the
# preserved backups — revoking it would strand them.
KEYCHAIN_SERVICES = [
    ("openrouter-api-key", "sanctum"),
    ("openrouter-mgmt-key", "sanctum"),
    ("openrouter-mgmt-key-backup", "sanctum"),
    ("anthropic-api-key", "sanctum"),
    ("google-ai-api-key", "sanctum"),   # was gemini-api-key (never written)
    ("r2-account-id", "sanctum"),
    ("r2-access-key-id", "sanctum"),
    ("r2-secret-access-key", "sanctum"),
    ("b2-account-id", "sanctum"),        # was b2-application-key-id
    ("b2-application-key", "sanctum"),
    ("bell-hub-admin", "admin"),         # default device-admin entries
    ("orbi-admin", "admin"),
]


# ─── Shared single-item teardown primitives ──────────────────────────
#
# These are the smallest reversible/destructive units. The global uninstall
# loops below call them, and ``commands/module.py`` reuses them as the real
# adapters behind injected callables so the teardown logic lives in one place.


def bootout_label(label: str, domain: str | None = None) -> None:
    """Bootout one LaunchAgent/Daemon label from the given launchd domain.

    Args:
        label:  The launchd label (e.g. ``com.sanctum.backup``).
        domain: The launchd domain prefix.  Defaults to ``gui/<uid>`` (the
                current user's GUI domain) when None — preserving the original
                global-uninstall behavior for existing call sites that omit it.

    Idempotent at the launchctl level: booting out a label that is not
    loaded is a no-op (non-zero rc, ignored). Touches launchd only — does
    NOT rename or delete any plist file.
    """
    resolved_domain = domain if domain is not None else f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", f"{resolved_domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )


def revoke_keychain_entry(account: str, service: str) -> bool:
    """Delete one generic-password entry. Returns True iff it existed + was
    deleted (rc==0). Missing entries return False (not an error)."""
    result = subprocess.run(
        ["security", "delete-generic-password", "-a", account, "-s", service],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def rename_with_suffix(path: Path, suffix: str) -> bool:
    """Rename *path* by appending *suffix* (recoverable, not a delete).

    Returns True iff the path existed and the rename succeeded. A
    ``{date}`` token in *suffix* is expanded to today's UTC YYYY-MM-DD.
    """
    if not path.exists():
        return False
    rendered = suffix.replace("{date}", datetime.now(UTC).strftime("%Y-%m-%d"))
    try:
        path.rename(path.with_name(path.name + rendered))
    except OSError as exc:
        console.print(f"  [yellow]could not rename {path}: {exc}[/]")
        return False
    return True


def _bootout_and_delete_launchagents() -> list[str]:
    """Bootout every com.sanctum.*.plist in ~/Library/LaunchAgents/ and
    rename them with a .uninstalled-YYYY-MM-DD suffix. Returns list of
    labels touched."""
    la_dir = Path.home() / "Library/LaunchAgents"
    if not la_dir.is_dir():
        return []
    touched = []
    ts = datetime.now(UTC).strftime("%Y-%m-%d")
    for plist in sorted(la_dir.glob("com.sanctum.*.plist")):
        if ".retired" in plist.name or ".disabled" in plist.name:
            continue
        label = plist.stem
        # Bootout (ok if not loaded).
        bootout_label(label)
        # Rename rather than delete — recoverable.
        plist.rename(plist.with_suffix(f".plist.uninstalled-{ts}"))
        touched.append(label)
    return touched


def _revoke_keychain_entries() -> list[str]:
    """Delete every known sanctum Keychain entry, each under its PAIRED account
    (not a hardcoded 'sanctum' — device-admin entries live under 'admin').
    Returns the services actually deleted (some may not exist on a partial
    install)."""
    deleted = []
    for service, account in KEYCHAIN_SERVICES:
        if revoke_keychain_entry(account, service):
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
    for app_name in ("SanctumBridge.app", "SanctumLauncher.app"):
        for parent in (Path.home() / "Applications", Path("/Applications")):
            app = parent / app_name
            if app.is_dir() and rename_with_suffix(app, ".uninstalled-{date}"):
                renamed.append(str(app))
    return renamed


def _purge_data_dirs() -> list[str]:
    """ONLY called when --purge is set. Removes the user-data dirs."""
    removed = []
    ts = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
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
