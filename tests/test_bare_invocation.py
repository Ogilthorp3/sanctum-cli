"""Bare ``sanctum`` — banner for pipes, council chamber for humans.

Typing ``sanctum`` with no subcommand prints the status one-liner and then,
ONLY when stdin+stdout are a real TTY, drops into the council REPL. Scripts,
sentinels, and pipes calling bare ``sanctum`` keep getting the banner and a
clean exit — an automation must never hang inside an interactive chamber.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sanctum_cli import cli
from sanctum_cli.commands import council as council_cmd

runner = CliRunner()


@pytest.fixture
def quiet_status(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    monkeypatch.setattr(
        cli.status,
        "status_command",
        lambda json_output=False, oneline=False: calls.append("banner"),
    )
    return calls


def test_bare_invocation_non_tty_prints_banner_only(
    quiet_status: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    entered: list[str] = []
    monkeypatch.setattr(council_cmd, "_repl", lambda: entered.append("repl"))
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: False)
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0
    assert quiet_status == ["banner"]
    assert entered == [], "non-TTY must never enter the chamber"


def test_bare_invocation_tty_enters_the_chamber(
    quiet_status: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    entered: list[str] = []
    monkeypatch.setattr(council_cmd, "_repl", lambda: entered.append("repl"))
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0
    assert quiet_status == ["banner"], "the banner still prints first — it is the HUD"
    assert entered == ["repl"]


def test_subcommands_unaffected(quiet_status: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    entered: list[str] = []
    monkeypatch.setattr(council_cmd, "_repl", lambda: entered.append("repl"))
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert entered == []
