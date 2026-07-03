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
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()


# (service, account) — the account matters: most live under 'sanctum', but the
# restic passphrase lives under 'sanctum-backup'. Names verified against the real
# write sites (backends/b2.py, onboard.py, config.py) — the old list had drifted:
# 'gemini-api-key' was never written (it's google-ai-api-key) and
# 'b2-application-key-id' should be 'b2-account-id', so BOTH were silently absent
# from the bundle. The restic passphrase was missing entirely — without it a DR
# bundle can't decrypt the very backups it pairs with.
KEYCHAIN_SERVICES = [
    ("openrouter-api-key", "sanctum"),
    ("openrouter-mgmt-key", "sanctum"),
    ("openrouter-mgmt-key-backup", "sanctum"),
    ("anthropic-api-key", "sanctum"),
    ("google-ai-api-key", "sanctum"),          # was gemini-api-key (never written)
    ("r2-account-id", "sanctum"),
    ("r2-access-key-id", "sanctum"),
    ("r2-secret-access-key", "sanctum"),
    ("b2-account-id", "sanctum"),              # was b2-application-key-id
    ("b2-application-key", "sanctum"),
    ("sanctum-backup-key", "sanctum-backup"),  # restic passphrase — decrypts the backups
]


def _read_keychain(service: str, account: str = "sanctum") -> str | None:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
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

    if not yes and not typer.confirm("Continue?", default=True):
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
            "created": datetime.now(UTC).isoformat(),
            "host": os.uname().nodename,
            "services": [],
        }
        captured = 0
        for service, account in KEYCHAIN_SERVICES:
            value = _read_keychain(service, account)
            if value is None:
                continue
            (bundle_dir / service).write_text(value, encoding="utf-8")
            (bundle_dir / service).chmod(0o600)
            manifest["services"].append({
                "service": service,
                "account": account,  # restore must write it back to the right account
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
        out.chmod(0o600)

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


def _write_keychain(service: str, account: str, value: str) -> bool:
    """security add-generic-password -U (update-in-place). True on success."""
    r = subprocess.run(
        ["security", "add-generic-password", "-a", account, "-s", service,
         "-w", value, "-U"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def keys_restore_command(
    path: Annotated[Path, typer.Argument(help="The encrypted bundle from `keys backup`.")],
) -> None:
    """Decrypt a `keys backup` bundle and write every entry back into this Mac's
    Keychain, each under the (service, account) recorded in the manifest."""
    if not path.is_file():
        console.print(f"[red]No such bundle:[/] {path}")
        raise typer.Exit(code=1)
    p1 = getpass.getpass("Passphrase for the bundle: ")
    with tempfile.TemporaryDirectory() as td:
        tar_path = Path(td) / "bundle.tar.gz"
        r = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
             "-in", str(path), "-out", str(tar_path), "-pass", "stdin"],
            input=p1, text=True, capture_output=True,
        )
        if r.returncode != 0:
            console.print("[red]Decryption failed[/] — wrong passphrase or corrupt bundle.")
            raise typer.Exit(code=1)
        subprocess.run(["tar", "-xzf", str(tar_path), "-C", str(td)], check=True)
        bundle_dir = Path(td) / "sanctum-keys"
        manifest_path = bundle_dir / "MANIFEST.json"
        if not manifest_path.is_file():
            console.print("[red]Bundle has no MANIFEST.json[/] — cannot restore.")
            raise typer.Exit(code=1)
        manifest = json.loads(manifest_path.read_text())
        restored, failed = 0, []
        for entry in manifest.get("services", []):
            service = entry["service"]
            account = entry.get("account", "sanctum")  # older bundles default
            value_file = bundle_dir / service
            if not value_file.is_file():
                failed.append(service)
                continue
            value = value_file.read_text(encoding="utf-8")
            if _write_keychain(service, account, value):
                restored += 1
            else:
                failed.append(service)
    console.print()
    body = f"[bold green]Restored {restored} key(s) into the Keychain.[/]"
    if failed:
        body += f"\n[yellow]{len(failed)} failed:[/] {', '.join(failed)}"
    console.print(Panel(body, border_style="green" if not failed else "yellow", padding=(1, 2)))
