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

from sanctum_cli import config, keychain, recipes
from sanctum_cli.errors import LocalError, UserError

console = Console()
err = Console(stderr=True)

DEFAULT_BACKUP_SCRIPT = Path("~/Backups/sanctum-backup.sh").expanduser()
RESTIC_TIMEOUT_S = 30
SNAPSHOTS_TIMEOUT_S = 15
BACKUP_TIMEOUT_S = 60 * 60  # 1h cap on a recipe-driven backup
ESTIMATE_TIMEOUT_S = 60
R2_FREE_TIER_GB = 10

RepoFilter = Literal["primary", "secondary", "all"]


@dataclass(frozen=True, slots=True)
class _Repo:
    label: str
    path: str


def _resolve_recipe_target(cfg: config.Config, recipe: config.Recipe) -> _Repo:
    """Return the cloud_backup repo the recipe targets."""
    cb = cfg.cli.cloud_backup
    if cb is None:
        msg = "no cloud_backup configured in instance.yaml"
        raise UserError(
            msg, fix="run `sanctum cloud setup` to configure a backup target"
        )
    slot = recipe.target
    repo_cfg = cb.primary if slot == "primary" else cb.secondary
    if repo_cfg is None:
        msg = f"recipe targets cloud_backup.{slot}, but it's not configured"
        raise UserError(msg, fix=f"run `sanctum cloud setup` to populate the {slot} slot")
    return _Repo(label=slot, path=repo_cfg.path if False else repo_cfg.repo)


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


def _restic_env(cfg: config.Config, repo: "_Repo") -> dict[str, str]:
    """Env for a restic invocation against ``repo``: RESTIC_PASSWORD plus the
    cloud-backend credentials restic needs to actually reach the repo. Without
    the cloud creds, run/verify/restore/snapshots against b2:/s3:/r2: repos
    fail with an auth error even though the passphrase is present.
    Credentials come from the same Keychain entries the wizard wrote (constants
    imported from the backends, not re-hardcoded)."""
    from sanctum_cli.backends import b2 as _b2
    from sanctum_cli.backends import r2 as _r2

    env = dict(os.environ)
    env["RESTIC_PASSWORD"] = _load_password(cfg)
    path = repo.path
    if path.startswith("b2:"):
        env["B2_ACCOUNT_ID"] = keychain.read(
            account=_b2.KEYCHAIN_ACCOUNT, service=_b2.KEYCHAIN_SERVICE_KEY_ID
        )
        env["B2_ACCOUNT_KEY"] = keychain.read(
            account=_b2.KEYCHAIN_ACCOUNT, service=_b2.KEYCHAIN_SERVICE_APP_KEY
        )
    elif path.startswith(("s3:", "r2:")):
        env["AWS_ACCESS_KEY_ID"] = keychain.read(
            account=_r2.KEYCHAIN_ACCOUNT, service=_r2.KEYCHAIN_SERVICE_R2_ACCESS_KEY
        )
        env["AWS_SECRET_ACCESS_KEY"] = keychain.read(
            account=_r2.KEYCHAIN_ACCOUNT, service=_r2.KEYCHAIN_SERVICE_R2_SECRET
        )
        env["AWS_DEFAULT_REGION"] = "auto"
    return env


# ─── run ────────────────────────────────────────────────────────────


def backup_run(
    recipe: Annotated[
        str | None,
        typer.Option(
            "--recipe",
            "-r",
            help="Backup recipe (family | operator | code | <user-defined>). "
            "If omitted, runs the legacy ~/Backups/sanctum-backup.sh.",
        ),
    ] = None,
    script: Annotated[
        Path | None,
        typer.Option(
            "--script",
            help=f"Path to backup script. Default: {DEFAULT_BACKUP_SCRIPT}.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be backed up without writing."),
    ] = False,
) -> None:
    """Run a backup. With --recipe, drives restic directly from a recipe;
    otherwise invokes the legacy bash script for backwards compatibility."""
    if recipe is not None:
        _backup_recipe(recipe, dry_run=dry_run)
        return

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


def _backup_recipe(name: str, *, dry_run: bool) -> None:
    """Run an in-CLI restic backup driven by a recipe."""
    cfg = config.load()
    recipe = recipes.resolve(name, cfg.cli)
    repo = _resolve_recipe_target(cfg, recipe)
    sources = recipes.expand_sources(recipe)
    if not sources:
        msg = f"recipe {name!r} has no resolvable sources on this host"
        raise UserError(
            msg,
            fix="check the recipe's `sources:` paths exist (or add them to instance.yaml)",
        )
    excludes = recipes.effective_excludes(recipe)

    if not shutil.which("restic"):
        msg = "restic not installed"
        raise LocalError(msg, fix="brew install restic")

    env = _restic_env(cfg, repo)

    # Write excludes to a temp file (restic --exclude-file)
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", prefix="sanctum-excludes-", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        for pattern in excludes:
            fh.write(pattern + "\n")
        excludes_path = Path(fh.name)

    try:
        cmd = ["restic", "-r", repo.path, "backup"]
        if dry_run:
            cmd.append("--dry-run")
        cmd.extend(["--tag", "daily", "--tag", recipe_tag(name)])
        cmd.extend(["--exclude-file", str(excludes_path), "--exclude-caches"])
        cmd.extend(str(s) for s in sources)

        console.print(
            f"[bold]recipe[/] [cyan]{name}[/] · "
            f"target={repo.label} ({repo.path}) · "
            f"sources={len(sources)} · excludes={len(excludes)}"
            + (" · [yellow]DRY RUN[/]" if dry_run else "")
        )
        for s in sources:
            console.print(f"  [dim]+[/] {s}")

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        )
        if proc.stdout is None:  # pragma: no cover
            msg = "restic stdout pipe missing"
            raise LocalError(msg)
        for line in proc.stdout:
            console.print(line.rstrip())
        rc = proc.wait(timeout=BACKUP_TIMEOUT_S)
        if rc != 0:
            msg = f"restic backup failed with rc={rc}"
            raise LocalError(msg)
        console.print(
            f"[green]✓[/] recipe {name!r} backup complete"
            + (" (dry run — nothing written)" if dry_run else "")
        )
    finally:
        excludes_path.unlink(missing_ok=True)


def recipe_tag(name: str) -> str:
    """Stable restic tag for the recipe — for `restic snapshots --tag`."""
    return f"recipe:{name}"


# ─── recipes (list) ──────────────────────────────────────────────────


def backup_recipes(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List available backup recipes (built-in + user-defined)."""
    cfg = config.load()
    rows = recipes.list_all(cfg.cli)
    overridden = set(cfg.cli.recipes.keys())

    if json_output:
        print(
            json.dumps(
                {
                    name: {
                        "description": r.description,
                        "sources": r.sources,
                        "excludes_count": len(r.excludes),
                        "target": r.target,
                        "auto_exclude_icloud_photos": r.auto_exclude_icloud_photos,
                        "user_override": name in overridden,
                    }
                    for name, r in rows.items()
                },
                indent=2,
            )
        )
        return

    t = Table(title="backup recipes", show_header=True, header_style="bold")
    t.add_column("name")
    t.add_column("origin", justify="right")
    t.add_column("target", justify="right")
    t.add_column("sources", justify="right")
    t.add_column("description")
    for name, r in rows.items():
        origin = "[yellow]override[/]" if name in overridden else "[dim]built-in[/]"
        t.add_row(name, origin, r.target, str(len(r.sources)), r.description)
    console.print(t)
    default = recipes.default_name(cfg.cli)
    console.print(f"\n[dim]default recipe:[/] [bold]{default}[/]")


# ─── estimate ────────────────────────────────────────────────────────


def backup_estimate(
    recipe: Annotated[
        str,
        typer.Argument(help="Recipe name (family | operator | code | <user>)."),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Estimate the on-disk size a recipe would back up before dedup.

    Uses ``du`` per-source — fast, doesn't read pack-level data. Doesn't
    account for restic dedup, so the number is an upper bound. After the
    first snapshot exists, dedup typically gives a 30-70% reduction on
    subsequent backups.
    """
    cfg = config.load()
    rcp = recipes.resolve(recipe, cfg.cli)
    sources = recipes.expand_sources(rcp)
    excludes = recipes.effective_excludes(rcp)

    if not sources:
        msg = f"recipe {recipe!r} has no resolvable sources on this host"
        raise UserError(msg)

    if not shutil.which("du"):
        msg = "du not installed"
        raise LocalError(msg)

    per_source: list[dict[str, object]] = []
    total_kb = 0
    for src in sources:
        # `du -sk` returns size in KB. Excludes via -I requires GNU du; macOS
        # du doesn't support it. We approximate by post-filtering find output.
        try:
            out = subprocess.run(
                ["du", "-sk", str(src)],
                capture_output=True,
                text=True,
                timeout=ESTIMATE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            per_source.append({"path": str(src), "size_kb": None, "note": "timeout"})
            continue
        if out.returncode != 0:
            per_source.append(
                {"path": str(src), "size_kb": None, "note": out.stderr.strip()[:80]}
            )
            continue
        kb = int(out.stdout.split("\t", 1)[0])
        total_kb += kb
        per_source.append({"path": str(src), "size_kb": kb})

    total_gb = total_kb / 1024 / 1024
    fits_free_tier = total_gb < R2_FREE_TIER_GB

    if json_output:
        print(
            json.dumps(
                {
                    "recipe": recipe,
                    "target": rcp.target,
                    "total_kb": total_kb,
                    "total_gb": round(total_gb, 2),
                    "fits_r2_free_tier": fits_free_tier,
                    "free_tier_gb": R2_FREE_TIER_GB,
                    "sources": per_source,
                    "excludes_count": len(excludes),
                    "note": "raw size pre-dedup; first snapshot ~= this, subsequent typically 30-70% smaller",
                },
                indent=2,
            )
        )
        return

    t = Table(title=f"estimate · recipe={recipe}", show_header=True, header_style="bold")
    t.add_column("source")
    t.add_column("size", justify="right")
    t.add_column("note")
    for s in per_source:
        size = (
            f"{s['size_kb'] / 1024 / 1024:.2f} GB"
            if isinstance(s["size_kb"], int)
            else "?"
        )
        t.add_row(str(s["path"]), size, str(s.get("note", "")))
    console.print(t)
    console.print()
    color = "green" if fits_free_tier else "yellow"
    console.print(
        f"[bold]total raw[/]   {total_gb:.2f} GB pre-dedup\n"
        f"[bold]R2 free[/]    {R2_FREE_TIER_GB} GB · "
        f"[{color}]{'fits' if fits_free_tier else 'EXCEEDS'}[/] free tier"
    )
    console.print(
        "[dim]first snapshot ~= raw size; subsequent backups typically 30-70% smaller after dedup.[/]"
    )


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

    snaps: list[_Snap] = []
    for r in repos:
        # Per-repo env: primary and secondary can be different backends
        # (b2 + r2), each needing its own cloud creds.
        snaps.extend(_restic_snapshots(r, _restic_env(cfg, r)))

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

    failed: list[str] = []
    for r in repos:
        console.print(f"[bold]{r.label}[/] {r.path}")
        env = _restic_env(cfg, r)  # per-repo cloud creds
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
    env = _restic_env(cfg, repos[0])

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
