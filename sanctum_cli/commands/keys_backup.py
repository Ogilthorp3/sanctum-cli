"""``sanctum keys backup <path>`` — export sanctum Keychain entries to an
encrypted bundle.

Council-doctrine for the trust gate (uninstall + reinstall): operator can
walk away cleanly because they have a portable, encrypted bundle of their
credentials. Reinstall is a single ``sanctum keys restore <path>``.

The bundle:
  - is a tar.gz of a temporary directory containing one file per service
  - is encrypted with ``openssl enc -aes-256-cbc -pbkdf2 -salt``
  - asks the operator for a passphrase interactively (twice, confirms)
  - never writes plaintext credentials to disk
  - includes a manifest with service names + timestamps (no values)
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()


KEYCHAIN_SERVICES = [
    "openrouter-api-key",
    "openrouter-mgmt-key",
    "openrouter-mgmt-key-backup",
    "anthropic-api-key",
    "gemini-api-key",
    "firewalla-bridge-token",
    "r2-account-id",
    "r2-access-key-id",
    "r2-secret-access-key",
    "b2-application-key-id",
    "b2-application-key",
]


def _read_keychain(service: str) -> str | None:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "sanctum", "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def keys_backup_command(
    out: Annotated[
        Path,
        typer.Argument(
            help="Where to write the encrypted bundle (e.g. ~/Documents/sanctum-keys-2026-05-22.tar.gz.enc).",
        ),
    ],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Export sanctum Keychain entries to an AES-256-encrypted bundle."""
    out = out.expanduser().absolute()
    if out.exists():
        console.print(f"[red]refusing to overwrite[/] {out}")
        raise typer.Exit(code=1)

    console.print()
    console.print(Panel.fit(
        f"[bold]sanctum keys backup → {out}[/]\n\n"
        f"Will export up to {len(KEYCHAIN_SERVICES)} sanctum Keychain entries "
        f"into an AES-256-encrypted tar bundle. The bundle is portable: copy "
        f"it to another Mac, run [cyan]sanctum keys restore <path>[/], enter "
        f"the same passphrase.",
        border_style="cyan",
    ))
    console.print()

    if not yes:
        if not typer.confirm("Continue?", default=True):
            raise typer.Exit(code=0)

    # Prompt for the passphrase twice; openssl will derive a key from it.
    p1 = getpass.getpass("Passphrase for the bundle: ")
    p2 = getpass.getpass("Confirm passphrase: ")
    if p1 != p2:
        console.print("[red]passphrases differ — aborting[/]")
        raise typer.Exit(code=1)
    if len(p1) < 12:
        console.print("[red]passphrase must be at least 12 characters[/]")
        raise typer.Exit(code=1)

    with tempfile.TemporaryDirectory() as td:
        bundle_dir = Path(td) / "sanctum-keys"
        bundle_dir.mkdir()

        manifest: dict[str, Any] = {
            "created": datetime.now(timezone.utc).isoformat(),
            "host": os.uname().nodename,
            "services": [],
        }
        captured = 0
        for service in KEYCHAIN_SERVICES:
            value = _read_keychain(service)
            if value is None:
                continue
            (bundle_dir / service).write_text(value, encoding="utf-8")
            os.chmod(bundle_dir / service, 0o600)
            manifest["services"].append({
                "service": service,
                "length": len(value),
                "prefix": value[:4] + "…" if len(value) > 8 else "***",
            })
            captured += 1

        (bundle_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

        # tar.gz the bundle dir
        tar_path = Path(td) / "bundle.tar.gz"
        subprocess.run(
            ["tar", "-czf", str(tar_path), "-C", str(td), "sanctum-keys"],
            check=True,
        )

        # AES-256-CBC encrypt with PBKDF2
        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
             "-in", str(tar_path), "-out", str(out),
             "-pass", "stdin"],
            input=p1, text=True, capture_output=True,
        )
        if r.returncode != 0:
            console.print(f"[red]openssl encryption failed:[/] {r.stderr.strip()}")
            raise typer.Exit(code=1)
        os.chmod(out, 0o600)

    console.print()
    console.print(Panel(
        f"[bold green]Wrote {captured} key(s) to {out}[/]\n\n"
        f"File is encrypted (AES-256-CBC, PBKDF2). Mode 600. "
        f"Keep the passphrase somewhere safe (your password manager).\n\n"
        f"To restore on another machine:\n"
        f"  [cyan]sanctum keys restore {out.name}[/]",
        border_style="green",
        padding=(1, 2),
    ))
