"""``sanctum status`` — health snapshot.

Reads ``instance.yaml``, queries restic for the most recent snapshot
across both repos, runs ``df`` for disk pressure, and aggregates a 7-day
telemetry summary from the JSONL log. All probes are bounded with
explicit timeouts; any single probe failure degrades that field to
``UNKNOWN`` rather than failing the whole command.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from sanctum_cli import config
from sanctum_cli.errors import LocalError

console = Console()

DF_PATH = "/System/Volumes/Data"
DF_TIMEOUT_S = 2
RESTIC_TIMEOUT_S = 5


@dataclass(frozen=True, slots=True)
class DiskInfo:
    used_pct: int | None
    used_gb: int | None
    free_gb: int | None
    total_gb: int | None
    status: str  # OPERATIONAL | DEGRADED | FAILED | UNKNOWN


@dataclass(frozen=True, slots=True)
class BackupInfo:
    repo: str
    last_snapshot_iso: str | None
    age_human: str | None
    snapshot_count: int | None
    status: str  # OPERATIONAL | DEGRADED | FAILED | UNKNOWN
    detail: str | None


@dataclass(frozen=True, slots=True)
class TelemetrySummary:
    window_days: int
    request_count: int
    error_count: int


# ─── Probes ─────────────────────────────────────────────────────────


def probe_disk() -> DiskInfo:
    if not shutil.which("df"):
        return DiskInfo(None, None, None, None, "UNKNOWN")
    try:
        out = subprocess.run(
            ["df", "-k", DF_PATH],
            capture_output=True,
            text=True,
            timeout=DF_TIMEOUT_S,
            check=True,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return DiskInfo(None, None, None, None, "UNKNOWN")

    lines = out.stdout.strip().split("\n")
    if len(lines) < 2:
        return DiskInfo(None, None, None, None, "UNKNOWN")
    cols = lines[1].split()
    if len(cols) < 5:
        return DiskInfo(None, None, None, None, "UNKNOWN")

    try:
        total_kb = int(cols[1])
        used_kb = int(cols[2])
        free_kb = int(cols[3])
        used_pct = int(cols[4].rstrip("%"))
    except ValueError:
        return DiskInfo(None, None, None, None, "UNKNOWN")

    status = "OPERATIONAL"
    if used_pct >= 90:
        status = "FAILED"
    elif used_pct >= 85:
        status = "DEGRADED"
    return DiskInfo(
        used_pct=used_pct,
        used_gb=round(used_kb / 1024 / 1024),
        free_gb=round(free_kb / 1024 / 1024),
        total_gb=round(total_kb / 1024 / 1024),
        status=status,
    )


def probe_backup(repo: str, password_env: dict[str, str]) -> BackupInfo:
    if not shutil.which("restic"):
        return BackupInfo(repo, None, None, None, "UNKNOWN", "restic not installed")
    try:
        out = subprocess.run(
            ["restic", "-r", repo, "snapshots", "--json", "--latest", "1", "--no-lock"],
            capture_output=True,
            text=True,
            timeout=RESTIC_TIMEOUT_S,
            check=False,
            env=password_env,
        )
    except subprocess.TimeoutExpired:
        return BackupInfo(repo, None, None, None, "UNKNOWN", "restic timed out")

    if out.returncode != 0:
        detail = (out.stderr.strip().splitlines() or ["restic failed"])[-1][:120]
        return BackupInfo(repo, None, None, None, "FAILED", detail)

    try:
        snaps = json.loads(out.stdout)
    except json.JSONDecodeError:
        return BackupInfo(repo, None, None, None, "UNKNOWN", "unparseable restic output")
    if not snaps:
        return BackupInfo(repo, None, None, 0, "DEGRADED", "no snapshots yet")

    latest = max(snaps, key=lambda s: s["time"])
    iso = latest["time"]
    age = _human_age(iso)
    status = _backup_status_from_age(iso)
    return BackupInfo(
        repo=repo,
        last_snapshot_iso=iso,
        age_human=age,
        snapshot_count=len(snaps),
        status=status,
        detail=None,
    )


def probe_telemetry(path: Path, window_days: int) -> TelemetrySummary:
    requests = 0
    errors = 0
    if not path.exists():
        return TelemetrySummary(window_days, 0, 0)
    cutoff = datetime.now(tz=UTC).timestamp() - window_days * 86_400
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = event.get("ts", "")
            try:
                ts_epoch = datetime.fromisoformat(ts).timestamp()
            except (ValueError, TypeError):
                continue
            if ts_epoch < cutoff:
                continue
            requests += 1
            if event.get("status") == "error":
                errors += 1
    return TelemetrySummary(window_days, requests, errors)


# ─── Helpers ────────────────────────────────────────────────────────


def _human_age(iso: str) -> str:
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = datetime.now(tz=UTC) - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def _backup_status_from_age(iso: str) -> str:
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return "UNKNOWN"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    seconds = (datetime.now(tz=UTC) - ts).total_seconds()
    if seconds < 36 * 3600:
        return "OPERATIONAL"
    if seconds < 72 * 3600:
        return "DEGRADED"
    return "FAILED"


def _password_env(cfg: config.Config) -> dict[str, str]:
    """Pull restic passphrase from Keychain if cloud_backup configured."""
    import os as _os

    env = dict(_os.environ)
    cb = cfg.cli.cloud_backup
    if cb is None or cb.primary is None:
        return env
    try:
        from sanctum_cli import keychain

        env["RESTIC_PASSWORD"] = keychain.read(
            account=cb.primary.keychain.account, service=cb.primary.keychain.service
        )
    except LocalError:
        # Probe failure surfaces in the per-repo status — not fatal here.
        pass
    return env


# ─── Aggregator ─────────────────────────────────────────────────────


def collect(cfg: config.Config) -> dict[str, Any]:
    disk = probe_disk()
    env = _password_env(cfg)
    backups: list[BackupInfo] = []
    cb = cfg.cli.cloud_backup
    if cb is not None:
        if cb.primary is not None:
            backups.append(probe_backup(cb.primary.repo, env))
        if cb.secondary is not None:
            backups.append(probe_backup(cb.secondary.repo, env))
    telemetry = probe_telemetry(
        Path(cfg.cli.telemetry.path).expanduser(),
        cfg.cli.telemetry.aggregate_window_days,
    )
    return {
        "instance": cfg.instance.name,
        "host": socket.gethostname(),
        "default_provider": cfg.cli.default_provider,
        "disk": disk,
        "backups": backups,
        "telemetry": telemetry,
    }


# ─── Renderers ──────────────────────────────────────────────────────


def render_oneline(snap: dict[str, Any]) -> str:
    disk = snap["disk"]
    backups = snap["backups"]
    bk_summary = "no-backup-config"
    if backups:
        ages = [b.age_human or "?" for b in backups]
        bk_summary = "backup " + " / ".join(ages)
    disk_pct = f"{disk.used_pct}%" if disk.used_pct is not None else "?%"
    return (
        f"sanctum @ {snap['instance']} · {bk_summary} · "
        f"router→{snap['default_provider']} · disk {disk_pct} ({disk.status.lower()}) · "
        f"telemetry {snap['telemetry'].request_count}r/{snap['telemetry'].error_count}e/"
        f"{snap['telemetry'].window_days}d"
    )


def render_table(snap: dict[str, Any]) -> None:
    disk = snap["disk"]
    tel = snap["telemetry"]

    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold cyan")
    header.add_column()
    header.add_row("instance", snap["instance"])
    header.add_row("host", snap["host"])
    header.add_row("default provider", snap["default_provider"])
    console.print(header)
    console.print()

    disk_table = Table(title="disk", show_header=True, header_style="bold")
    disk_table.add_column("path")
    disk_table.add_column("used")
    disk_table.add_column("free")
    disk_table.add_column("total")
    disk_table.add_column("status", justify="right")
    disk_table.add_row(
        DF_PATH,
        f"{disk.used_gb} GB ({disk.used_pct}%)" if disk.used_gb is not None else "?",
        f"{disk.free_gb} GB" if disk.free_gb is not None else "?",
        f"{disk.total_gb} GB" if disk.total_gb is not None else "?",
        _color(disk.status),
    )
    console.print(disk_table)
    console.print()

    if snap["backups"]:
        bk = Table(title="backups", show_header=True, header_style="bold")
        bk.add_column("repo")
        bk.add_column("snapshots", justify="right")
        bk.add_column("age", justify="right")
        bk.add_column("status", justify="right")
        bk.add_column("detail")
        for b in snap["backups"]:
            bk.add_row(
                _abbrev(b.repo, 50),
                str(b.snapshot_count) if b.snapshot_count is not None else "?",
                b.age_human or "?",
                _color(b.status),
                b.detail or "",
            )
        console.print(bk)
        console.print()

    console.print(f"telemetry: {tel.request_count} requests · {tel.error_count} errors · last {tel.window_days}d")


def render_json(snap: dict[str, Any]) -> str:
    out = {
        "instance": snap["instance"],
        "host": snap["host"],
        "default_provider": snap["default_provider"],
        "disk": _dataclass_dict(snap["disk"]),
        "backups": [_dataclass_dict(b) for b in snap["backups"]],
        "telemetry": _dataclass_dict(snap["telemetry"]),
    }
    return json.dumps(out, indent=2)


def _dataclass_dict(obj: object) -> dict[str, Any]:
    """Convert frozen dataclass to plain dict (no copies of None)."""
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is None:
        return {}
    return {k: getattr(obj, k) for k in fields}


def _color(status: str) -> str:
    return {
        "OPERATIONAL": "[green]OPERATIONAL[/]",
        "DEGRADED": "[yellow]DEGRADED[/]",
        "FAILED": "[red]FAILED[/]",
        "UNKNOWN": "[dim]UNKNOWN[/]",
    }.get(status, status)


def _abbrev(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ─── Entry ──────────────────────────────────────────────────────────


def status_command(*, json_output: bool, oneline: bool) -> None:
    cfg = config.load()
    snap = collect(cfg)
    if json_output:
        print(render_json(snap))
    elif oneline:
        print(render_oneline(snap))
    else:
        render_table(snap)
