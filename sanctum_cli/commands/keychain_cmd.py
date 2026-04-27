"""``sanctum keychain`` — list / rotate / test the entries sanctum cares about.

The list operation never prints values — only (account, service, exists?).
Rotation generates a fresh secret and replaces the entry with -U.
Test reads to confirm the entry is currently accessible (handles the
locked-keychain case explicitly).
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from dataclasses import dataclass
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from sanctum_cli import config, keychain
from sanctum_cli.errors import LocalError

console = Console()

SECURITY_BIN = "/usr/bin/security"


@dataclass(frozen=True, slots=True)
class _Entry:
    label: str  # human-readable role
    service: str
    account: str


def _registry(cfg: config.Config) -> list[_Entry]:
    """Return the Keychain entries sanctum manages, derived from instance.yaml."""
    entries: list[_Entry] = [
        _Entry("Anthropic API key", cfg.cli.providers.claude.keychain.service, cfg.cli.providers.claude.keychain.account),
        _Entry("Google AI API key", cfg.cli.providers.gemini.keychain.service, cfg.cli.providers.gemini.keychain.account),
    ]
    cb = cfg.cli.cloud_backup
    if cb is not None and cb.primary is not None:
        entries.append(
            _Entry("restic passphrase (primary)", cb.primary.keychain.service, cb.primary.keychain.account)
        )
    if cb is not None and cb.secondary is not None:
        entries.append(
            _Entry("restic passphrase (secondary)", cb.secondary.keychain.service, cb.secondary.keychain.account)
        )
    return entries


def keychain_list(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List the Keychain entries sanctum cares about. Values are never printed."""
    cfg = config.load()
    rows = _registry(cfg)
    annotated = [
        {
            "label": e.label,
            "service": e.service,
            "account": e.account,
            "exists": keychain.exists(account=e.account, service=e.service),
        }
        for e in rows
    ]
    if json_output:
        import json as _json

        print(_json.dumps(annotated, indent=2))
        return
    t = Table(title="Keychain (sanctum-managed)", show_header=True, header_style="bold")
    t.add_column("label")
    t.add_column("service")
    t.add_column("account")
    t.add_column("status", justify="right")
    for r in annotated:
        t.add_row(
            str(r["label"]),
            str(r["service"]),
            str(r["account"]),
            "[green]present[/]" if r["exists"] else "[red]missing[/]",
        )
    console.print(t)


def keychain_test() -> None:
    """Read every managed entry to confirm Keychain is unlocked + entries exist."""
    cfg = config.load()
    failures: list[str] = []
    for e in _registry(cfg):
        try:
            keychain.read(account=e.account, service=e.service)
            console.print(f"[green]✓[/] {e.service} / {e.account}")
        except LocalError as exc:
            failures.append(f"{e.service}: {exc.message}")
            console.print(f"[red]✗[/] {e.service} / {e.account} — {exc.message}")
    if failures:
        msg = f"{len(failures)} keychain entr{'y' if len(failures) == 1 else 'ies'} unreadable"
        raise LocalError(msg)


def keychain_rotate(
    service: Annotated[str, typer.Argument(help="Keychain service name to rotate.")],
    account: Annotated[
        str | None,
        typer.Option("--account", "-a", help="Account name. Defaults to 'sanctum'."),
    ] = None,
    new_value: Annotated[
        str | None,
        typer.Option(
            "--value",
            help="Provide the new value. Omit to auto-generate a 64-char hex secret.",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompt.")
    ] = False,
) -> None:
    """Replace a Keychain entry with a new value. Auto-generates 64 hex chars by default."""
    if not shutil.which(SECURITY_BIN):
        msg = f"missing {SECURITY_BIN}"
        raise LocalError(msg)
    acct = account or "sanctum"
    value = new_value or secrets.token_hex(32)

    if not yes:
        from rich.prompt import Confirm

        confirm = Confirm.ask(
            f"Rotate Keychain entry [bold]{service}[/] (account={acct})?", default=False
        )
        if not confirm:
            console.print("[dim]aborted[/]")
            raise typer.Exit(code=0)

    proc = subprocess.run(
        [SECURITY_BIN, "add-generic-password", "-a", acct, "-s", service, "-w", value, "-U"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = f"rotation failed: {proc.stderr.strip() or 'unknown error'}"
        raise LocalError(msg)

    console.print(
        f"[green]✓[/] rotated {service} / {acct}; "
        f"new value is {len(value)} chars long, kept in Keychain only."
    )
    if new_value is None:
        # Auto-generated: print so the operator can mirror to 1Password
        from rich.panel import Panel

        console.print(
            Panel.fit(
                f"[bold]copy this to 1Password now — it won't be shown again[/]\n\n{value}",
                border_style="yellow",
            )
        )
