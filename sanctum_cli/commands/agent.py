"""``sanctum agent`` — manage com.sanctum.* LaunchAgents.

Wraps ``launchctl bootstrap``/``bootout``/``list`` with typed errors and
an HTTP-quality status table. ``logs`` follows the StandardOutPath /
StandardErrorPath declared in the plist (parsed via ``plutil -convert
json``), avoiding hardcoded log locations.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.table import Table

from sanctum_cli.errors import LocalError, UserError

console = Console()

LAUNCHCTL_BIN = "/bin/launchctl"
PLUTIL_BIN = "/usr/bin/plutil"
LAUNCHCTL_TIMEOUT_S = 5
PLIST_LOCATIONS = [
    Path("~/Library/LaunchAgents").expanduser(),
    Path("/Library/LaunchAgents"),
]
SANCTUM_PREFIX = "com.sanctum."

Status = Literal["RUNNING", "LOADED", "FAILED", "MISSING"]


@dataclass(frozen=True, slots=True)
class AgentRow:
    label: str
    pid: str
    last_exit: str
    status: Status


def _launchctl_list() -> list[AgentRow]:
    if not shutil.which(LAUNCHCTL_BIN):
        return []
    try:
        out = subprocess.run(
            [LAUNCHCTL_BIN, "list"],
            capture_output=True,
            text=True,
            timeout=LAUNCHCTL_TIMEOUT_S,
            check=True,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return []
    rows: list[AgentRow] = []
    for raw in out.stdout.splitlines()[1:]:
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        pid, last_exit, label = parts[0], parts[1], parts[2]
        if not label.startswith(SANCTUM_PREFIX):
            continue
        rows.append(
            AgentRow(label=label, pid=pid, last_exit=last_exit, status=_compute_status(pid, last_exit))
        )
    rows.sort(key=lambda r: r.label)
    return rows


def _compute_status(pid: str, last_exit: str) -> Status:
    if pid not in ("-", "0"):
        return "RUNNING"
    try:
        rc = int(last_exit)
    except ValueError:
        return "LOADED"
    if rc == 0:
        return "LOADED"
    return "FAILED"


def _color(status: str) -> str:
    return {
        "RUNNING": "[green]RUNNING[/]",
        "LOADED": "[cyan]LOADED[/]",
        "FAILED": "[red]FAILED[/]",
        "MISSING": "[dim]MISSING[/]",
    }.get(status, status)


def _resolve_plist(label: str) -> Path:
    for base in PLIST_LOCATIONS:
        candidate = base / f"{label}.plist"
        if candidate.exists():
            return candidate
    msg = f"plist not found for {label} in {[str(p) for p in PLIST_LOCATIONS]}"
    raise UserError(msg, fix="install the plist or pass a custom path")


def _read_plist(plist: Path) -> dict[str, object]:
    if shutil.which(PLUTIL_BIN):
        proc = subprocess.run(
            [PLUTIL_BIN, "-convert", "json", "-o", "-", str(plist)],
            capture_output=True,
            text=True,
            timeout=LAUNCHCTL_TIMEOUT_S,
            check=False,
        )
        if proc.returncode == 0:
            parsed: dict[str, object] = json.loads(proc.stdout)
            return parsed
    # Fallback for Linux / when plutil isn't present
    fallback: dict[str, object] = dict(plistlib.loads(plist.read_bytes()))
    return fallback


def _gui_target() -> str:
    return f"gui/{os.getuid()}"


# ─── public commands ───────────────────────────────────────────────


def agent_list(json_output: bool = False) -> None:
    rows = _launchctl_list()
    if json_output:
        print(
            json.dumps(
                [
                    {"label": r.label, "pid": r.pid, "last_exit": r.last_exit, "status": r.status}
                    for r in rows
                ],
                indent=2,
            )
        )
        return
    if not rows:
        console.print("[dim]no com.sanctum.* agents loaded[/]")
        return
    t = Table(title=f"com.sanctum.* LaunchAgents ({len(rows)})", show_header=True, header_style="bold")
    t.add_column("label")
    t.add_column("pid", justify="right")
    t.add_column("last exit", justify="right")
    t.add_column("status", justify="right")
    for r in rows:
        t.add_row(r.label, r.pid, r.last_exit, _color(r.status))
    console.print(t)


def agent_status(label: str) -> None:
    rows = _launchctl_list()
    row = next((r for r in rows if r.label == label), None)
    if row is None:
        console.print(f"[red]{label}[/] not loaded")
        msg = f"{label} not in launchctl list"
        raise UserError(msg, fix=f"sanctum agent start {label}")
    console.print(f"[bold]{row.label}[/]")
    console.print(f"  pid:       {row.pid}")
    console.print(f"  last_exit: {row.last_exit}")
    console.print(f"  status:    {_color(row.status)}")
    plist = _resolve_plist(label)
    console.print(f"  plist:     {plist}")
    info = _read_plist(plist)
    out_path = info.get("StandardOutPath")
    err_path = info.get("StandardErrorPath")
    if out_path:
        console.print(f"  stdout →   {out_path}")
    if err_path and err_path != out_path:
        console.print(f"  stderr →   {err_path}")


def _run_launchctl(args: list[str]) -> None:
    proc = subprocess.run(
        [LAUNCHCTL_BIN, *args],
        capture_output=True,
        text=True,
        timeout=LAUNCHCTL_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        msg = f"launchctl {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()[:160]}"
        raise LocalError(msg)


def agent_start(label: str) -> None:
    plist = _resolve_plist(label)
    _run_launchctl(["bootstrap", _gui_target(), str(plist)])
    console.print(f"[green]✓[/] bootstrapped {label}")


def agent_stop(label: str) -> None:
    _run_launchctl(["bootout", f"{_gui_target()}/{label}"])
    console.print(f"[green]✓[/] booted out {label}")


def agent_restart(label: str) -> None:
    plist = _resolve_plist(label)
    # Bootout is allowed to fail (e.g., not currently loaded)
    subprocess.run(
        [LAUNCHCTL_BIN, "bootout", f"{_gui_target()}/{label}"],
        capture_output=True,
        text=True,
        timeout=LAUNCHCTL_TIMEOUT_S,
        check=False,
    )
    _run_launchctl(["bootstrap", _gui_target(), str(plist)])
    console.print(f"[green]✓[/] restarted {label}")


def agent_logs(
    label: str,
    *,
    follow: bool = False,
    lines: int = 50,
) -> None:
    plist = _resolve_plist(label)
    info = _read_plist(plist)
    out_path = info.get("StandardOutPath")
    err_path = info.get("StandardErrorPath")
    log_paths: list[Path] = []
    if out_path:
        log_paths.append(Path(str(out_path)).expanduser())
    if err_path and err_path != out_path:
        log_paths.append(Path(str(err_path)).expanduser())
    if not log_paths:
        msg = f"{label} has no StandardOutPath / StandardErrorPath in its plist"
        raise UserError(msg, fix="add a StandardOutPath to the plist or check `agent status`")

    existing = [p for p in log_paths if p.exists()]
    if not existing:
        console.print("[dim]no log files exist yet (the agent may not have run)[/]")
        if not follow:
            return

    if not follow:
        for p in existing:
            console.print(f"[bold cyan]── {p} ──[/]")
            with p.open(encoding="utf-8", errors="replace") as fh:
                tail = _tail_lines(fh, lines)
                for line in tail:
                    console.print(line.rstrip())
        return

    # Follow mode: open all paths, seek to end, poll
    handles = [p.open(encoding="utf-8", errors="replace") for p in existing]
    try:
        for h in handles:
            h.seek(0, os.SEEK_END)
        console.print("[dim](following — Ctrl-C to stop)[/]")
        while True:
            wrote = False
            for p, h in zip(existing, handles, strict=False):
                line = h.readline()
                if line:
                    console.print(f"[dim]{p.name}[/] {line.rstrip()}")
                    wrote = True
            if not wrote:
                time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")
    finally:
        for h in handles:
            h.close()


def _tail_lines(fh, n: int) -> list[str]:  # type: ignore[no-untyped-def]
    """Return the last ``n`` lines of an open text file. Memory-bounded."""
    if n <= 0:
        return []
    lines: list[str] = []
    for line in fh:
        lines.append(line)
        if len(lines) > n:
            lines.pop(0)
    return lines


# Tiny re-export so other modules can use the typed Annotated cleanly
__all__ = [
    "AgentRow",
    "_color",
    "_launchctl_list",
    "agent_list",
    "agent_logs",
    "agent_restart",
    "agent_start",
    "agent_status",
    "agent_stop",
]
