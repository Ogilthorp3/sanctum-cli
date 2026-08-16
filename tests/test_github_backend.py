"""GitHub Tier 0 backend — gh + git mocked at the subprocess boundary."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.backends import github as gh_backend
from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_hostname_slug_normalizes() -> None:
    s = gh_backend._hostname_slug()
    assert s == s.lower()
    assert all(c.isalnum() or c == "-" for c in s)


def test_repo_name_format() -> None:
    assert gh_backend._repo_name("acme", "my-host") == "acme/sanctum-host-my-host"


def test_preflight_refuses_when_gh_missing() -> None:
    from sanctum_cli.errors import UserError

    with patch("sanctum_cli.backends.github.shutil.which", return_value=None):
        try:
            gh_backend._preflight()
        except UserError as exc:
            assert "gh CLI" in exc.message
        else:
            raise AssertionError("expected UserError")


def test_preflight_refuses_when_gh_unauthenticated() -> None:
    from sanctum_cli.errors import UserError

    def _which(name):  # type: ignore[no-untyped-def]
        return f"/usr/local/bin/{name}"

    with (
        patch("sanctum_cli.backends.github.shutil.which", side_effect=_which),
        patch(
            "sanctum_cli.backends.github.subprocess.run",
            return_value=_completed(rc=1, stderr="not logged in"),
        ),
    ):
        try:
            gh_backend._preflight()
        except UserError as exc:
            assert "not authenticated" in exc.message
        else:
            raise AssertionError("expected UserError")


def test_repo_exists_branches_on_gh_exit_code() -> None:
    with patch(
        "sanctum_cli.backends.github.subprocess.run",
        return_value=_completed(rc=0, stdout='{"name":"x"}'),
    ):
        assert gh_backend._repo_exists("acme/x") is True
    with patch(
        "sanctum_cli.backends.github.subprocess.run",
        return_value=_completed(rc=1, stderr="not found"),
    ):
        assert gh_backend._repo_exists("acme/x") is False


def test_full_wizard_refuses_when_secret_detected(
    minimal_instance_yaml: Path, monkeypatch, tmp_path: Path
):  # type: ignore[no-untyped-def]
    """A staged file that trips the secret scanner aborts the push."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    fake_clone = tmp_path / "clone"
    fake_clone.mkdir()
    (fake_clone / ".git").mkdir()  # pretend clone

    # Plant a leaky dotfile in the FAKE source list
    leaky = tmp_path / "leaky-zshrc"
    leaky.write_text(
        "export ANTHROPIC_API_KEY=sk-ant-api03-" + "Z" * 80 + "\n",
        encoding="utf-8",
    )

    with (
        patch("sanctum_cli.backends.github.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "sanctum_cli.backends.github.subprocess.run",
            return_value=_completed(stdout="ok"),
        ),
        patch("sanctum_cli.backends.github.LOCAL_CLONE_DIR", fake_clone),
        patch("sanctum_cli.backends.github.DEFAULT_SOURCES", [leaky]),
        patch("sanctum_cli.backends.github._capture_inventories", return_value=[]),
        patch("sanctum_cli.backends.github._copy_sanctum_launchagents", return_value=[]),
        patch("sanctum_cli.backends.github._ensure_clone", return_value=None),
        patch("sanctum_cli.backends.github._repo_exists", return_value=True),
        patch("sanctum_cli.backends.github.Confirm.ask", return_value=True),
    ):
        result = runner.invoke(app, ["cloud", "setup", "--backend", "github", "--no-persist"])

    assert result.exit_code == 1, result.stdout + (result.stderr or "")
    combined = result.stdout + (result.stderr or "")
    assert "secret" in combined.lower() or "refused" in combined.lower()


def test_full_wizard_happy_path_clean_dotfile(
    minimal_instance_yaml: Path, monkeypatch, tmp_path: Path
):  # type: ignore[no-untyped-def]
    """Clean dotfile — wizard succeeds, ``--no-persist`` skips the push."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    fake_clone = tmp_path / "clone"
    fake_clone.mkdir()
    (fake_clone / ".git").mkdir()

    clean = tmp_path / "zshrc"
    clean.write_text("alias ll='ls -la'\nexport PATH=/opt/homebrew/bin:$PATH\n", encoding="utf-8")

    with (
        patch("sanctum_cli.backends.github.shutil.which", return_value="/usr/local/bin/gh"),
        patch(
            "sanctum_cli.backends.github.subprocess.run",
            return_value=_completed(stdout="ok"),
        ),
        patch("sanctum_cli.backends.github.LOCAL_CLONE_DIR", fake_clone),
        patch("sanctum_cli.backends.github.DEFAULT_SOURCES", [clean]),
        patch("sanctum_cli.backends.github._capture_inventories", return_value=[]),
        patch("sanctum_cli.backends.github._copy_sanctum_launchagents", return_value=[]),
        patch("sanctum_cli.backends.github._ensure_clone", return_value=None),
        patch("sanctum_cli.backends.github._repo_exists", return_value=True),
    ):
        result = runner.invoke(app, ["cloud", "setup", "--backend", "github", "--no-persist"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "no secrets detected" in result.stdout.lower()
