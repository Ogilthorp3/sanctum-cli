"""Regression: GitHub Tier 0 must scan the FULL staged set before pushing.

``_commit_and_push`` runs ``git add -A``, which stages the entire persistent
clone — not just the files this run wrote. The wizard's Step-4 pre-scan only
covers this run's ``written`` list, so a secret that entered the clone some
other way (a prior run, a manual edit, an unrelated dropped file) would sail
past it and get pushed. The authoritative guard lives in ``_commit_and_push``:
scan ``git diff --cached`` and refuse before committing.

These tests exercise a REAL temp git repo (not mocked subprocess) so the
git-plumbing path is verified end to end.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from sanctum_cli.backends import github as gh_backend
from sanctum_cli.errors import UserError

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@sanctum.local")
    _git(repo, "config", "user.name", "Sanctum Test")
    # An initial clean commit → HEAD is born, mirroring a real persistent clone.
    (repo / "README.md").write_text("# host config\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")


def test_commit_and_push_scans_full_staged_set(tmp_path: Path) -> None:
    """A secret NOT in this run's ``written`` list, but staged by ``add -A``,
    must abort the push (raise UserError) and never get committed."""
    repo = tmp_path / "clone"
    _init_repo(repo)

    # A credential enters the clone some OTHER way — never seen by the
    # wizard's per-run pre-scan. AKIA + 16 upper-alnum = AWS access key id.
    (repo / "stray-creds.txt").write_text(
        "aws_access_key_id = AKIA" + "A" * 16 + "\n", encoding="utf-8"
    )

    with pytest.raises(UserError) as excinfo:
        gh_backend._commit_and_push(repo, "sanctum cloud sync")

    assert "refused" in excinfo.value.message.lower()

    # Only the initial clean commit exists → the secret was never committed,
    # so `git push` (which follows commit) was never reached.
    assert _git(repo, "rev-list", "--all", "--count").strip() == "1"
    # The staged set was unstaged (git reset); the file remains on disk for
    # the operator to remediate.
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""
    assert (repo / "stray-creds.txt").exists()


def test_commit_and_push_commits_clean_staged_set(tmp_path: Path) -> None:
    """Control: a clean staged change commits (push targets a real bare repo,
    so we prove the whole path works when there are no findings)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)

    repo = tmp_path / "clone"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(remote))
    # Push the initial commit so origin/HEAD exists.
    _git(repo, "push", "-q", "origin", "HEAD")

    (repo / "dotfiles.txt").write_text("alias ll='ls -la'\n", encoding="utf-8")

    pushed = gh_backend._commit_and_push(repo, "sanctum cloud sync")

    assert pushed is True
    assert _git(repo, "rev-list", "--all", "--count").strip() == "2"


def test_commit_and_push_stages_clean_files_on_finding(tmp_path: Path) -> None:
    """If findings are detected, stage only the clean files, and leave the dirty files unstaged."""
    repo = tmp_path / "clone"
    _init_repo(repo)

    (repo / "clean.txt").write_text("alias ll='ls -la'\n", encoding="utf-8")
    (repo / "dirty.txt").write_text("aws_access_key_id = AKIA" + "A" * 16 + "\n", encoding="utf-8")

    with pytest.raises(UserError) as excinfo:
        gh_backend._commit_and_push(repo, "sanctum cloud sync")

    assert "refused" in excinfo.value.message.lower()

    # The clean file must be staged
    staged = _git(repo, "diff", "--cached", "--name-only").strip().splitlines()
    assert "clean.txt" in staged
    assert "dirty.txt" not in staged

    # The dirty file remains in working tree, unstaged
    assert (repo / "dirty.txt").exists()
    unstaged = _git(repo, "status", "--porcelain").strip().splitlines()
    # It should be untracked or modified
    assert any("dirty.txt" in line and (line.startswith("??") or line.startswith(" M") or line.startswith("? ")) for line in unstaged)
