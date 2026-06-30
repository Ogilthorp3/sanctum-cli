"""``sanctum link`` — the Sanctum Link Optimizer (Measure + Diagnose slice).

Universal: every Sanctum node runs the stability sentinel and can read its own
verdict. This slice ships two commands:

* ``status``  — read the sentinel log, classify it, print verdict + metrics +
  remedy. Read-only; exits non-zero only on a real read error. A missing log is
  NOT an error — it prints a friendly NO_DATA hint and exits 0.
* ``install`` — write the sentinel sampler (0755) + its LaunchAgent and
  best-effort ``launchctl bootstrap`` it. Idempotent.

Later phases (optimize / SQM / failover) layer on top; they are deliberately not
built here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path  # noqa: TC003 - Typer resolves this annotation at runtime
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from sanctum_cli.errors import LocalError, SanctumError
from sanctum_cli.net import link

console = Console()
err_console = Console(stderr=True)

link_app = typer.Typer(help="Measure + diagnose the node's network link (Wi-Fi stability).")

_LAUNCHCTL_BIN = "/bin/launchctl"
_LAUNCHCTL_TIMEOUT_S = 5

# Verdict → Rich style, so a glance at the colour reads the health.
_VERDICT_STYLE: dict[str, str] = {
    "HEALTHY": "green",
    "LOAD": "yellow",
    "SCAN": "yellow",
    "RADIO": "red",
    "NO_DATA": "dim",
}


def _report(exc: SanctumError) -> None:
    """Pretty-print a SanctumError to stderr with its optional fix suggestion.

    Mirrors ``net._report`` so the link sub-app reports failures the same way the
    rest of the CLI does (it cannot import from ``cli`` without a cycle).
    """
    err_console.print(f"[bold red]error:[/] {exc.message}")
    if exc.fix:
        err_console.print(f"[dim]fix:[/] {exc.fix}")


def _render(diag: link.Diagnosis) -> None:
    """Print a diagnosis: verdict (coloured) + detail + metrics + remedy."""
    style = _VERDICT_STYLE.get(diag.verdict, "white")
    console.print(f"[bold]VERDICT:[/] [{style}]{escape(diag.verdict)}[/]")
    console.print(f"  {escape(diag.detail)}")
    m = diag.metrics
    if m is not None:
        console.print(
            f"  [dim]({m.samples} samples, {m.degraded_pct}% degraded, "
            f"p50 {m.p50_avg_ms}ms, worst {m.worst_avg_ms}ms, "
            f"loss {m.mean_loss_pct}%)[/]"
        )
    console.print(f"  → {escape(diag.remedy)}")


@link_app.command(
    "status",
    help="Diagnose link stability from the sentinel log (read-only).",
)
def link_status(
    log: Annotated[
        Path | None,
        typer.Option(
            "--log",
            help="Sentinel log path (default: ~/.sanctum/logs/wifi-stability.log).",
        ),
    ] = None,
) -> None:
    """Classify the sentinel log and print the verdict.

    A missing log is the expected fresh-install state, NOT a failure: print a
    NO_DATA hint pointing at ``sanctum link install`` and exit 0. Only a genuine
    read error (permission denied, log path is a directory) exits non-zero.
    """
    log_path = log if log is not None else link.default_log_path()
    try:
        text = log_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # No log yet → friendly NO_DATA, exit 0.
        console.print("[bold]VERDICT:[/] [dim]NO_DATA[/]")
        console.print(f"  no sentinel log at {escape(str(log_path))}")
        console.print(
            "  → Run [bold]sanctum link install[/] to start the stability "
            "sentinel, then re-run in a few minutes."
        )
        return
    except OSError as exc:
        err = LocalError(
            f"cannot read sentinel log {log_path}: {exc}",
            fix="check the path + permissions, or pass --log <file>.",
        )
        _report(err)
        raise typer.Exit(code=int(err.exit_code)) from exc

    _render(link.classify(link.parse_log(text)))


def _launchctl(args: list[str], *, check: bool) -> tuple[bool, str]:
    """Run ``launchctl`` once; return (ok, stderr-tail). Never raises.

    Module-level seam so ``install`` tests can stub launchctl without shelling
    out. ``check=False`` is used for the pre-emptive bootout (a not-loaded label
    returning non-zero is expected and ignored).
    """
    try:
        proc = subprocess.run(
            [_LAUNCHCTL_BIN, *args],
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (False, str(exc)[:160])
    ok = proc.returncode == 0 or not check
    return (ok, proc.stderr.strip()[:160])


def _bootstrap_sentinel(plist_path: Path) -> tuple[bool, str]:
    """Best-effort (re)load the sentinel LaunchAgent. Returns (loaded, detail).

    Idempotent: bootout any prior instance (failure ignored) then bootstrap the
    fresh plist into the per-user GUI domain, mirroring ``agent restart``.
    """
    target = f"gui/{os.getuid()}"
    label = link.SENTINEL_LABEL
    _launchctl(["bootout", f"{target}/{label}"], check=False)
    ok, detail = _launchctl(["bootstrap", target, str(plist_path)], check=True)
    if ok:
        return (True, f"bootstrapped {label}")
    return (False, detail or "launchctl bootstrap failed")


@link_app.command(
    "install",
    help="Install the Wi-Fi stability sentinel (script + LaunchAgent) on this node.",
)
def link_install() -> None:
    """Write the sentinel sampler + LaunchAgent and best-effort load it.

    Idempotent — re-running overwrites to the same end state. File writes are the
    real contract here; the ``launchctl`` load is best-effort and never aborts the
    command (status prints what actually happened).
    """
    script_path = link.sentinel_script_path()
    plist_path = link.sentinel_plist_path()
    err_path = link.default_err_path()
    log_path = link.default_log_path()

    try:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.parent.mkdir(parents=True, exist_ok=True)

        script_path.write_text(link.SENTINEL_SCRIPT, encoding="utf-8")
        script_path.chmod(0o755)

        plist_path.write_text(
            link.render_plist(script=script_path, err_log=err_path),
            encoding="utf-8",
        )
    except OSError as exc:
        err = LocalError(
            f"failed to install sentinel files: {exc}",
            fix="check that ~/.sanctum/bin and ~/Library/LaunchAgents are writable.",
        )
        _report(err)
        raise typer.Exit(code=int(err.exit_code)) from exc

    console.print(f"[green]✓[/] wrote sampler   {escape(str(script_path))} [dim](0755)[/]")
    console.print(f"[green]✓[/] wrote LaunchAgent {escape(str(plist_path))}")

    loaded, detail = _bootstrap_sentinel(plist_path)
    if loaded:
        console.print(
            f"[green]✓[/] {escape(detail)} "
            f"[dim](samples every {link.SENTINEL_INTERVAL_S}s → {log_path})[/]"
        )
    else:
        console.print(
            f"[yellow]![/] sentinel files installed but launchctl load was not "
            f"confirmed: {escape(detail)}"
        )
        console.print(
            f"  [dim]load it manually: launchctl bootstrap gui/$(id -u) "
            f"{escape(str(plist_path))}[/]"
        )
    console.print(
        f"[dim]Run [bold]sanctum link status[/] once samples accumulate "
        f"(~{link.SENTINEL_INTERVAL_S}s cadence).[/]"
    )
