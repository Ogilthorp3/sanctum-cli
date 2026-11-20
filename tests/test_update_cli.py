"""Regression tests for ``sanctum update`` (M-CLI-5)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import self_test as st
from sanctum_cli.commands import update as update_cmd

runner = CliRunner()


def test_update_spawns_subprocess_self_test_never_in_process(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fail_in_process(*_a: object, **_kw: object) -> None:
        raise AssertionError("must not call self_test_command in-process")

    def fake_which(name: str) -> str | None:
        if name == "brew":
            return "/opt/homebrew/bin/brew"
        if name == "sanctum":
            return "/opt/homebrew/bin/sanctum"
        return None

    monkeypatch.setattr(update_cmd.shutil, "which", fake_which)
    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)
    monkeypatch.setattr(st, "self_test_command", fail_in_process)

    res = runner.invoke(app, ["update"])
    assert res.exit_code == 0, res.output
    assert calls == [
        ["brew", "update"],
        ["brew", "upgrade", "ogilthorp3/sanctum/sanctum-cli"],
        ["/opt/homebrew/bin/sanctum", "self-test"],
    ]


def test_resolve_self_test_cmd_falls_back_to_sys_executable(monkeypatch) -> None:
    monkeypatch.setattr(update_cmd.shutil, "which", lambda _n: None)
    monkeypatch.setattr(Path, "is_file", lambda _self: False)
    assert update_cmd._resolve_self_test_cmd() == [sys.executable, "-m", "sanctum_cli", "self-test"]


def test_update_propagates_nonzero_self_test_exit(monkeypatch) -> None:
    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        rc = 1 if cmd[-1] == "self-test" else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")
    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)

    res = runner.invoke(app, ["update"])
    assert res.exit_code == 1
    assert "self-test failed" in res.output.lower()


def test_update_dry_run_and_skip_self_test_and_missing_brew(monkeypatch) -> None:
    monkeypatch.setattr(update_cmd.shutil, "which", lambda _n: None)
    res_dry = runner.invoke(app, ["update", "--dry-run"])
    assert res_dry.exit_code == 0

    res_missing = runner.invoke(app, ["update"])
    assert res_missing.exit_code == 1
    assert "brew" in res_missing.output.lower()

    calls: list[list[str]] = []
    monkeypatch.setattr(update_cmd.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda cmd, **_kw: (calls.append(list(cmd)), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
    )
    res_skip = runner.invoke(app, ["update", "--skip-self-test"])
    assert res_skip.exit_code == 0
    assert len(calls) == 2
