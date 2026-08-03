"""``sanctum wizard`` — the easter egg runs, exits 0, and lands the line."""

from __future__ import annotations

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import wizard


def test_wizard_runs_and_closes_with_the_line() -> None:
    result = CliRunner().invoke(app, ["wizard"])
    assert result.exit_code == 0
    assert "You're a wizard, Bert." in result.output
    assert "sanctum " in result.output  # the one honest status line rode along


def test_wizard_art_stays_small_and_wholesome() -> None:
    lines = [ln for ln in wizard.ART.splitlines() if ln.strip()]
    assert 8 <= len(lines) <= 12, "the robe stays tasteful — no sprawling murals"
