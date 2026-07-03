"""GitHub Tier 0 — public-safe configs to a free private repo.

The free GitHub plan supports unlimited private repos. For Sanctum
productization, this is the cheapest tier: the kind of stuff that's
easy to commit (dotfiles, app inventories, LaunchAgent plists) goes
here, leaving the R2 free tier (10 GB) free for the things that don't
belong in git (sensitive documents, secrets bundles).

Architecture:
  - One repo per host: ``<owner>/sanctum-host-<hostname-slug>``, where
    ``<owner>`` resolves from instance.yaml or the authenticated ``gh`` user.
  - Persistent local clone at ``~/.sanctum/cli/github-tier-0/``.
  - Staged sync: build a temp tree, scan for secrets, refuse on any
    match, then commit + push. The persistent clone keeps git history.
  - Auth via the ``gh`` CLI (the user's existing GitHub authentication).

Not a backup-of-last-resort — this is *configuration-as-code*. If the
machine dies, you clone the repo, run ``brew install $(cat brew.txt)``,
copy dotfiles, and you're back to a working shell in minutes.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from sanctum_cli import config, secret_scanner
from sanctum_cli.errors import LocalError, UserError

console = Console()

GH_BIN = "gh"
GIT_BIN = "git"
GH_TIMEOUT_S = 30
LOCAL_CLONE_DIR = Path("~/.sanctum/cli/github-tier-0").expanduser()


def _gh_login() -> str | None:
    """The authenticated GitHub login via ``gh api user``, or ``None`` if gh is
    unavailable / unauthenticated. No personal literal — discovery-first."""
    if not shutil.which(GH_BIN):
        return None
    proc = subprocess.run(
        [GH_BIN, "api", "user", "--jq", ".login"],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        return None
    login = proc.stdout.strip()
    return login or None


def default_owner() -> str:
    """GitHub owner for host-backup repos — no baked-in personal account.

    Resolution order (discovery-first):
      1. instance.yaml ``vcs.github_owner`` (explicit operator config);
      2. the authenticated GitHub user via ``gh api user``;
      3. raise :class:`UserError` — there is no personal fallback to ship.
    """
    configured = config.instance_value("vcs.github_owner", None)
    if configured:
        return str(configured)
    login = _gh_login()
    if login:
        return login
    msg = "could not resolve your GitHub account"
    raise UserError(
        msg,
        fix="set vcs.github_owner in ~/.sanctum/instance.yaml (or run `gh auth login`)",
    )

# Curated list of dotfiles + inventories that are safe-by-design. Anything
# in this list still gets secret-scanned before it lands in git, but the
# default sources should never trip the scanner on a clean install.
DEFAULT_SOURCES: list[Path] = [
    Path("~/.zshrc").expanduser(),
    Path("~/.zprofile").expanduser(),
    Path("~/.gitconfig").expanduser(),
    Path("~/.tmux.conf").expanduser(),
    Path("~/.vimrc").expanduser(),
]

LAUNCHAGENT_GLOB = Path("~/Library/LaunchAgents").expanduser()


@dataclass(frozen=True, slots=True)
class _SetupResult:
    repo: str
    clone_dir: Path
    files_synced: int


# ─── Pre-flight ─────────────────────────────────────────────────────


def _preflight() -> None:
    if not shutil.which(GH_BIN):
        msg = "gh CLI not installed"
        raise UserError(msg, fix="brew install gh && gh auth login")
    if not shutil.which(GIT_BIN):
        msg = "git not installed"
        raise UserError(msg, fix="install Xcode Command Line Tools or brew install git")
    proc = subprocess.run(
        [GH_BIN, "auth", "status"],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        msg = "gh CLI is not authenticated"
        raise UserError(msg, fix="run `gh auth login`")


# ─── Repo helpers ───────────────────────────────────────────────────


def _hostname_slug() -> str:
    raw = socket.gethostname().split(".", 1)[0].lower()
    return re.sub(r"[^a-z0-9-]", "-", raw)


def _repo_name(owner: str, hostname: str) -> str:
    return f"{owner}/sanctum-host-{hostname}"


def _repo_exists(repo: str) -> bool:
    proc = subprocess.run(
        [GH_BIN, "repo", "view", repo, "--json", "name"],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
        check=False,
    )
    return proc.returncode == 0


def _create_repo(repo: str) -> None:
    proc = subprocess.run(
        [GH_BIN, "repo", "create", repo, "--private", "--description", "Sanctum host configuration (Tier 0)"],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        msg = f"gh repo create failed: {proc.stderr.strip()[:200]}"
        raise UserError(msg)


def _ensure_clone(repo: str, target: Path) -> None:
    """Idempotent: clone if missing, fetch otherwise."""
    if (target / ".git").exists():
        proc = subprocess.run(
            [GIT_BIN, "-C", str(target), "fetch", "--all"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            msg = f"git fetch failed: {proc.stderr.strip()[:200]}"
            raise LocalError(msg)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [GH_BIN, "repo", "clone", repo, str(target)],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S * 4,  # initial clone can be slow
        check=False,
    )
    if proc.returncode != 0:
        msg = f"gh repo clone failed: {proc.stderr.strip()[:200]}"
        raise UserError(msg)


# ─── File staging ───────────────────────────────────────────────────


def _capture_inventories(target: Path) -> list[str]:
    """Generate brew/mas/Applications inventories into ``target`` (the clone
    dir). Returns the list of relative paths it wrote, for the commit."""
    written: list[str] = []
    inv_dir = target / "inventory"
    inv_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which("brew"):
        for variant, fname in [("--formula", "brew-formulae.txt"), ("--cask", "brew-casks.txt")]:
            proc = subprocess.run(
                ["brew", "list", variant],
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_S,
                check=False,
            )
            if proc.returncode == 0:
                (inv_dir / fname).write_text(proc.stdout, encoding="utf-8")
                written.append(f"inventory/{fname}")
    if shutil.which("mas"):
        proc = subprocess.run(
            ["mas", "list"], capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False
        )
        if proc.returncode == 0:
            (inv_dir / "mas-apps.txt").write_text(proc.stdout, encoding="utf-8")
            written.append("inventory/mas-apps.txt")
    apps = sorted(p.name for p in Path("/Applications").iterdir() if p.suffix == ".app")
    (inv_dir / "applications.txt").write_text("\n".join(apps) + "\n", encoding="utf-8")
    written.append("inventory/applications.txt")
    return written


def _copy_dotfiles(target: Path, sources: list[Path]) -> list[str]:
    """Copy each existing source into ``target``. Returns committed-relative paths."""
    written: list[str] = []
    home = Path.home()
    dotfiles_dir = target / "dotfiles"
    dotfiles_dir.mkdir(parents=True, exist_ok=True)
    for src in sources:
        if not src.exists() or not src.is_file():
            continue
        # Strip the leading dot so the file shows in `ls`; map ~/.zshrc → dotfiles/zshrc
        try:
            rel = src.relative_to(home)
        except ValueError:
            rel = Path(src.name)
        out = dotfiles_dir / str(rel).removeprefix(".")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(src.read_bytes())
        written.append(str(out.relative_to(target)))
    return written


def _copy_sanctum_launchagents(target: Path) -> list[str]:
    """Copy com.sanctum.*.plist files (no secrets per Sanctum doctrine)."""
    written: list[str] = []
    if not LAUNCHAGENT_GLOB.exists():
        return written
    out_dir = target / "launchagents"
    out_dir.mkdir(parents=True, exist_ok=True)
    for plist in sorted(LAUNCHAGENT_GLOB.glob("com.sanctum.*.plist")):
        out = out_dir / plist.name
        out.write_bytes(plist.read_bytes())
        written.append(f"launchagents/{plist.name}")
    return written


def _ensure_readme(target: Path, hostname: str, owner: str) -> str:
    readme = target / "README.md"
    if readme.exists():
        return "README.md"
    body = f"""# sanctum-host-{hostname}

Tier 0 of the Sanctum backup architecture: public-safe configuration
for this host, synced via the free GitHub private repo plan.

## Restore on a fresh machine

```bash
gh repo clone {owner}/sanctum-host-{hostname}
cd sanctum-host-{hostname}
brew install $(cat inventory/brew-formulae.txt | tr '\\n' ' ')
brew install --cask $(cat inventory/brew-casks.txt | tr '\\n' ' ')
cp dotfiles/zshrc ~/.zshrc
cp dotfiles/gitconfig ~/.gitconfig
# (etc.)
```

For the rest — secrets, documents, Sanctum repo state — see the
restic-encrypted Tier 1 in your R2 / GDrive bucket.

Generated by `sanctum cloud setup --backend github`.
"""
    readme.write_text(body, encoding="utf-8")
    return "README.md"


# ─── Commit + push ──────────────────────────────────────────────────


def _git(target: Path, *args: str, check: bool = True, timeout: int = GH_TIMEOUT_S) -> str:
    proc = subprocess.run(
        [GIT_BIN, "-C", str(target), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        msg = f"git {' '.join(args)} failed: {proc.stderr.strip()[:200]}"
        raise LocalError(msg)
    return proc.stdout


def _commit_and_push(target: Path, message: str) -> bool:
    _git(target, "add", "-A")
    status = _git(target, "status", "--porcelain", check=True)
    if not status.strip():
        return False
    # Authoritative pre-push scan. ``git add -A`` stages the ENTIRE persistent
    # clone, not just this run's files — a secret that entered the clone some
    # other way (a prior run, a manual edit, an unrelated file dropped in the
    # working tree) would ride along on the push. The wizard's Step-4 pre-scan
    # only covers this run's ``written`` list, so it is UX, not a guarantee.
    # Scan the actual staged set here and refuse before committing.
    staged = _git(target, "diff", "--cached", "--name-only", "-z", check=True)
    staged_paths = [target / rel for rel in staged.split("\0") if rel]
    findings = secret_scanner.scan_paths(staged_paths)
    if findings:
        # Unstage everything (leave the working tree intact) so a re-run after
        # remediation starts clean; then refuse.
        _git(target, "reset", check=False)
        _render_findings(findings)
        msg = f"refused to push: {len(findings)} secret-scanner finding(s) in staged set"
        raise UserError(
            msg,
            fix=(
                "remove the offending content (or move it to your R2 bucket "
                "via `sanctum backup run --recipe family`) and re-run."
            ),
        )
    _git(target, "commit", "-m", message)
    _git(target, "push", "origin", "HEAD", timeout=GH_TIMEOUT_S * 4)
    return True


# ─── Wizard ─────────────────────────────────────────────────────────


def run_wizard(*, persist: bool = True, owner: str | None = None) -> _SetupResult:
    owner = owner or default_owner()
    _preflight()
    hostname = _hostname_slug()
    repo = _repo_name(owner, hostname)
    clone_dir = LOCAL_CLONE_DIR

    console.print(
        Panel.fit(
            "[bold]Sanctum cloud-setup wizard — GitHub Tier 0[/]\n\n"
            "Public-safe configuration (dotfiles, app inventories, sanctum "
            "LaunchAgent plists) goes to a [bold]free private GitHub repo[/], "
            "leaving the R2 free tier (10 GB) for the things that don't "
            "belong in git.\n\n"
            "Pre-commit secret-scanner refuses to push anything matching "
            "well-known credential patterns. False positives are easier "
            "to forgive than leaks.",
            border_style="cyan",
        )
    )

    if not _repo_exists(repo):
        console.print(f"\n[bold]Step 1.[/] Creating private repo [cyan]{repo}[/] …")
        _create_repo(repo)
        console.print("  [green]✓[/] repo created")
    else:
        console.print(f"\n[bold]Step 1.[/] Repo [cyan]{repo}[/] already exists; reusing.")

    console.print(f"\n[bold]Step 2.[/] Cloning to [cyan]{clone_dir}[/] …")
    _ensure_clone(repo, clone_dir)
    console.print("  [green]✓[/] clone ready")

    console.print("\n[bold]Step 3.[/] Staging files …")
    written: list[str] = []
    written.extend(_copy_dotfiles(clone_dir, DEFAULT_SOURCES))
    written.extend(_copy_sanctum_launchagents(clone_dir))
    written.extend(_capture_inventories(clone_dir))
    written.append(_ensure_readme(clone_dir, hostname, owner))
    console.print(f"  [green]✓[/] {len(written)} files staged")

    console.print("\n[bold]Step 4.[/] Pre-commit secret scan …")
    paths_to_scan = [clone_dir / p for p in written]
    findings = secret_scanner.scan_paths(paths_to_scan)
    if findings:
        _render_findings(findings)
        msg = f"refused to push: {len(findings)} secret-scanner finding(s)"
        raise UserError(
            msg,
            fix=(
                "remove the offending content (or move it to your R2 bucket "
                "via `sanctum backup run --recipe family`) and re-run."
            ),
        )
    console.print("  [green]✓[/] no secrets detected")

    if not persist:
        console.print(
            "\n[yellow]--no-persist:[/] skipping commit + push. Files staged in "
            f"[dim]{clone_dir}[/]."
        )
        return _SetupResult(repo=repo, clone_dir=clone_dir, files_synced=len(written))

    console.print("\n[bold]Step 5.[/] Committing + pushing …")
    if not Confirm.ask("  push to GitHub now?", default=True):
        console.print("  [dim]aborted by user; staged files remain in the clone[/]")
        return _SetupResult(repo=repo, clone_dir=clone_dir, files_synced=len(written))

    pushed = _commit_and_push(
        clone_dir, message=f"sanctum cloud sync from {socket.gethostname()}"
    )
    if pushed:
        console.print("  [green]✓[/] pushed to origin/HEAD")
    else:
        console.print("  [dim]nothing to commit — repo already up to date[/]")

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan")
    summary.add_column()
    summary.add_row("repo", repo)
    summary.add_row("local clone", str(clone_dir))
    summary.add_row("files synced", str(len(written)))
    summary.add_row("scope", "dotfiles + brew/mas inventories + sanctum LaunchAgents")
    console.print()
    console.print(Panel.fit(summary, title="[bold green]done[/]", border_style="green"))
    return _SetupResult(repo=repo, clone_dir=clone_dir, files_synced=len(written))


def _render_findings(findings: list[secret_scanner.Finding]) -> None:
    t = Table(
        title="[red]secret-scanner findings — refusing to push[/]",
        show_header=True,
        header_style="bold red",
    )
    t.add_column("file")
    t.add_column("pattern")
    t.add_column("location")
    t.add_column("snippet")
    for f in findings:
        t.add_row(str(f.path), f.pattern, f.location, f.snippet)
    console.print(t)
