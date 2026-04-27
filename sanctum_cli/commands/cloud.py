"""``sanctum cloud setup`` — guided wizard for cloud backup targets.

v0.3 ships Backblaze B2 (recommended default — no OAuth, just a key + secret).
Drive lands in v0.4 once the OAuth-gate handling is polished.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sanctum_cli.backends import b2, gdrive, r2
from sanctum_cli.errors import UserError


def cloud_setup_command(
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help="Backend to configure: r2 (recommended — egress-free) | b2 | gdrive.",
        ),
    ] = "r2",
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Do not auto-open browser tabs."),
    ] = False,
    no_persist: Annotated[
        bool,
        typer.Option("--no-persist", help="Print resulting YAML instead of editing instance.yaml."),
    ] = False,
) -> None:
    """Walk through cloud backup configuration."""
    if backend == "r2":
        r2.run_wizard(auto_open=not no_open, persist=not no_persist)
        return
    if backend == "b2":
        b2.run_wizard(auto_open=not no_open, persist=not no_persist)
        return
    if backend == "gdrive":
        gdrive.run_wizard(auto_open=not no_open, persist=not no_persist)
        return
    msg = f"unknown backend: {backend!r} (expected: r2 | b2 | gdrive)"
    raise UserError(msg)
