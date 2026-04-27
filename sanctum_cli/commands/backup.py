"""``sanctum backup`` — thin shims around the user's restic repos.

The actual backup work is done by ``~/Backups/sanctum-backup.sh`` (or
whatever path the operator configures). This module just provides the
typed CLI surface — list snapshots, run a backup, verify integrity,
restore — so operators don't have to remember the restic incantations.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from sanctum_cli import config, keychain
from sanctum_cli.errors import LocalError, UserError

console = Console()
err = Console(stderr=True)

DEFAULT_BACKUP_SCRIPT = Path("~/Backups/sanctum-backup.sh").expanduser()
RESTIC_TIMEOUT_S = 30
SNAPSHOTS_TIMEOUT_S = 15

RepoFilter = Literal["primary", "secondary", "all"]


@dataclass(frozen=True, slots=True)
class _Repo:
    label: str
    path: str


def _resolve_repos(cfg: config.Config, repo_filter: RepoFilter) -> list[_Repo]:
    cb = cfg.cli.cloud_backup
    if cb is None:
        msg = "no cloud_backup configured in instance.yaml"
        raise UserError(
            msg,
            fix="run `sanctum cloud setup` to configure a backup target",
        )
    repos: list[_Repo] = []
    if repo_filter in ("primary", "all") and cb.primary is not None:
        repos.append(_Repo(label="primary", path=cb.primary.repo))
    if repo_filter in ("secondary", "all") and cb.secondary is not None:
        repos.append(_Repo(label="secondary", path=cb.secondary.repo))
    if not repos:
        msg = f"no {repo_filter} repo configured"
        raise UserError(msg, fix="check instance.yaml cli.cloud_backup")
    return repos


def _load_password(cfg: config.Config) -> str:
    cb = cfg.cli.cloud_backup
    if cb is None or cb.primary is None:
        msg = "cannot find Keychain pointer (cloud_backup.primary missing)"
        raise UserError(msg)
    return keychain.read(
        account=cb.primary.keychain.account, service=cb.primary.keychain.service
    )


# ─── run ────────────────────────────────────────────────────────────


def backup_run(
    script: Annotated[
        Path | None,
        typer.Option(
            "--script",
            help=f"Path to backup script. Default: {DEFAULT_BACKUP_SCRIPT}",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Invoke the configured backup script and stream its output."""
    target = script or DEFAULT_BACKUP_SCRIPT
    if not target.exists():
        msg = f"backup script not found: {target}"
        raise UserError(msg, fix="pass --script <path> or create the default script")
    proc = subprocess.Popen(
        ["/bin/bash", str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if proc.stdout is None:  # pragma: no cover - defensive
        msg = "subprocess stdout pipe missing"
        raise LocalError(msg)
    for line in proc.stdout:
        console.print(line.rstrip())
    rc = proc.wait()
    if rc != 0:
        msg = f"backup script exited with code {rc}"
        raise LocalError(msg)


# ─── snapshots ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Snap:
    repo_label: str
    snap_id: str
    time: str
    paths: list[str]
    tags: list[str]


def _restic_snapshots(repo: _Repo, env: dict[str, str]) -> list[_Snap]:
    if not shutil.which("restic"):
        msg = "restic not installed"
        raise LocalError(msg, fix="brew install restic")
    out = subprocess.run(
        ["restic", "-r", repo.path, "snapshots", "--json", "--no-lock"],
        capture_output=True,
        text=True,
        timeout=SNAPSHOTS_TIMEOUT_S,
        check=False,
        env=env,
    )
    if out.returncode != 0:
        last = (out.stderr.strip().splitlines() or ["restic failed"])[-1]
        msg = f"restic snapshots failed for {repo.label}: {last}"
        raise LocalError(msg)
    data = json.loads(out.stdout or "[]")
    return [
        _Snap(
            repo_label=repo.label,
            snap_id=s.get("short_id", s.get("id", "?"))[:8],
            time=s.get("time", "?"),
            paths=s.get("paths", []) or [],
            tags=s.get("tags", []) or [],
        )
        for s in data
    ]


def backup_snapshots(
    repo: Annotated[
        str,
        typer.Option(
            "--repo",
            help="Which repo: primary | secondary | all.",
        ),
    ] = "all",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List snapshots from configured restic repos."""
    cfg = config.load()
    repos = _resolve_repos(cfg, _validate_repo(repo))
    env = dict(os.environ)
    env["RESTIC_PASSWORD"] = _load_password(cfg)

    snaps: list[_Snap] = []
    for r in repos:
        snaps.extend(_restic_snapshots(r, env))

    if json_output:
        print(
            json.dumps(
                [
                    {
                        "repo": s.repo_label,
                        "id": s.snap_id,
                        "time": s.time,
                        "paths": s.paths,
                        "tags": s.tags,
                    }
                    for s in snaps
                ],
                indent=2,
            )
        )
        return

    if not snaps:
        console.print("[dim]no snapshots[/]")
        return
    t = Table(title="restic snapshots", show_header=True, header_style="bold")
    t.add_column("repo")
    t.add_column("id")
    t.add_column("time")
    t.add_column("tags")
    for s in snaps:
        t.add_row(s.repo_label, s.snap_id, s.time, ", ".join(s.tags))
    console.print(t)


# ─── verify ─────────────────────────────────────────────────────────


def backup_verify(
    repo: Annotated[
        str,
        typer.Option("--repo", help="Which repo: primary | secondary | all."),
    ] = "all",
) -> None:
    """Run ``restic check`` against the configured repos."""
    cfg = config.load()
    repos = _resolve_repos(cfg, _validate_repo(repo))
    env = dict(os.environ)
    env["RESTIC_PASSWORD"] = _load_password(cfg)

    failed: list[str] = []
    for r in repos:
        console.print(f"[bold]{r.label}[/] {r.path}")
        proc = subprocess.run(
            ["restic", "-r", r.path, "check", "--no-lock"],
            text=True,
            timeout=RESTIC_TIMEOUT_S * 4,  # check is heavier
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            failed.append(r.label)
            err.print(f"[red]✗[/] {r.label} verification failed (rc={proc.returncode})")
        else:
            console.print(f"[green]✓[/] {r.label} verified")
    if failed:
        msg = f"verification failed for: {', '.join(failed)}"
        raise LocalError(msg, fix="run `restic -r <repo> rebuild-index` then re-verify")


# ─── restore ────────────────────────────────────────────────────────


def backup_restore(
    snapshot: Annotated[str, typer.Argument(help="Snapshot id (short or full).")],
    target: Annotated[Path, typer.Argument(help="Directory to restore into.")],
    repo: Annotated[
        str,
        typer.Option("--repo", help="Which repo: primary | secondary."),
    ] = "primary",
) -> None:
    """Restore a snapshot to a target directory."""
    cfg = config.load()
    if repo not in ("primary", "secondary"):
        msg = f"--repo must be primary or secondary, got {repo!r}"
        raise UserError(msg)
    repos = _resolve_repos(cfg, repo)  # type: ignore[arg-type]
    repo_path = repos[0].path
    env = dict(os.environ)
    env["RESTIC_PASSWORD"] = _load_password(cfg)

    target.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["restic", "-r", repo_path, "restore", snapshot, "--target", str(target)],
        text=True,
        timeout=RESTIC_TIMEOUT_S * 60,  # restore can be slow
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        msg = f"restic restore failed (rc={proc.returncode})"
        raise LocalError(msg)
    console.print(f"[green]✓[/] restored {snapshot} → {target}")


def _validate_repo(name: str) -> RepoFilter:
    if name not in ("primary", "secondary", "all"):
        msg = f"--repo must be primary, secondary, or all (got {name!r})"
        raise UserError(msg)
    return name  # type: ignore[return-value]
