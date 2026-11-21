"""The sub-apps that cannot import ``cli._report`` print errors the way it does.

``net``, ``link``, ``tailnet``, ``mesh`` and ``vault-armor`` each keep a local reporter (an
import of ``cli`` would be a cycle). Each prints its ``error:`` and ``fix:`` lines as Text,
never as Rich markup, so a path or value in brackets (``[site]``, ``"[bold]x"``) prints as
written instead of being swallowed as a style tag; and soft-wrapped, so a long path stays
whole on its line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import typer

from sanctum_cli.commands import link, mesh, net, tailnet, vault_armor_cmd
from sanctum_cli.errors import ConfigError, ExitCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from sanctum_cli.errors import SanctumError

_LONG_PATH = "/Volumes/[site]/" + "a-long-directory-name/" * 6 + "instance.yaml"
_ERROR = ConfigError(
    f'cannot read {_LONG_PATH}: "[bold]x" on line 4 is not a valid date',
    fix="quote it as '[b]2026-02-30[/b]', then re-run \\",
)


def _vault_armor(exc: SanctumError) -> None:
    with pytest.raises(typer.Exit) as exited:
        vault_armor_cmd._report_and_exit(exc)
    assert exited.value.exit_code == ExitCode.CONFIG_ERROR


_REPORTERS: dict[str, Callable[[SanctumError], None]] = {
    "net": net._report,
    "link": link._report,
    "tailnet": tailnet._report,
    "mesh": mesh._report,
    "vault-armor": _vault_armor,
}


@pytest.mark.parametrize("module", sorted(_REPORTERS))
def test_error_and_fix_print_exactly_as_written(
    module: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _REPORTERS[module](_ERROR)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [f"error: {_ERROR.message}", f"fix: {_ERROR.fix}"]


@pytest.mark.parametrize("module", sorted(_REPORTERS))
def test_an_error_without_a_fix_prints_one_line(
    module: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _REPORTERS[module](ConfigError("[red]plain[/red] message"))
    assert capsys.readouterr().err.splitlines() == ["error: [red]plain[/red] message"]
