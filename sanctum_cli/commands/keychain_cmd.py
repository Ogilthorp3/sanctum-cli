"""``sanctum keychain`` — list / rotate / test the entries sanctum cares about.

The list operation never prints values — only (account, service, exists?).
Rotation generates a fresh secret and replaces the entry with -U.
Test reads to confirm the entry is currently accessible (handles the
locked-keychain case explicitly).
"""

from __future__ import annotations

import getpass
import secrets
import sys
from dataclasses import dataclass
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from sanctum_cli import config, keychain
from sanctum_cli.errors import LocalError, UserError

console = Console()


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
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt",
            "-p",
            help="Prompt interactively for the new value (masked; never on argv).",
        ),
    ] = False,
    value_stdin: Annotated[
        bool,
        typer.Option(
            "--value-stdin",
            help="Read the new value from stdin (requires --yes).",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation prompt.")
    ] = False,
) -> None:
    """Replace a Keychain entry with a new value. Auto-generates 64 hex chars by default."""
    if prompt and value_stdin:
        raise UserError(
            "pass either --prompt or --value-stdin, not both",
            fix="choose one input channel",
        )
    if value_stdin and not yes:
        raise UserError(
            "--value-stdin requires --yes so confirmation does not consume stdin",
            fix="re-run with --value-stdin --yes",
        )

    acct = account or "sanctum"
    auto_generated = not prompt and not value_stdin
    if value_stdin:
        value = sys.stdin.read().rstrip("\r\n")
        if not value:
            raise UserError("empty value read from stdin")
    elif prompt:
        v1 = getpass.getpass("New Keychain value: ")
        v2 = getpass.getpass("Confirm new value: ")
        if v1 != v2:
            raise UserError("entered values differ")
        if not v1:
            raise UserError("new Keychain value must not be empty")
        value = v1
    else:
        value = secrets.token_hex(32)

    if not yes:
        from rich.prompt import Confirm

        confirm = Confirm.ask(
            f"Rotate Keychain entry [bold]{service}[/] (account={acct})?", default=False
        )
        if not confirm:
            console.print("[dim]aborted[/]")
            raise typer.Exit(code=0)

    try:
        keychain.write(account=acct, service=service, value=value, update=True)
    except LocalError as exc:
        msg = f"rotation failed: {exc.message}"
        raise LocalError(msg, fix=exc.fix) from exc

    console.print(
        f"[green]✓[/] rotated {service} / {acct}; "
        f"new value is {len(value)} chars long, kept in Keychain only."
    )
    if auto_generated:
        # Auto-generated: print so the operator can mirror to 1Password
        from rich.panel import Panel

        console.print(
            Panel.fit(
                f"[bold]copy this to 1Password now — it won't be shown again[/]\n\n{value}",
                border_style="yellow",
            )
        )
