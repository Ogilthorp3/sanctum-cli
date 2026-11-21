"""Tests for hive service principal (wave-1) checks."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sanctum_cli import service_user as su
from sanctum_cli.cli import app
from sanctum_cli.commands import service_user_cmd  # noqa: F401 — register


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_cli_only_not_applicable(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "haus_tier_present", lambda: False)
    report = su.check_wave1()
    assert report.applicable is False
    assert report.ok is True


def test_wave1_fail_when_user_missing(monkeypatch):
    monkeypatch.setattr(su, "haus_tier_present", lambda: True)
    monkeypatch.setattr(su, "service_user_exists", lambda name="sanctum": False)
    monkeypatch.setattr(su, "_plist_username", lambda p: None)
    monkeypatch.setattr(su, "_process_owner", lambda p: None)
    monkeypatch.setattr(su, "_http_status", lambda *a, **k: 0)
    monkeypatch.setattr(su, "DAEMON_DIR", Path("/nonexistent/LaunchDaemons"))
    report = su.check_wave1()
    assert report.applicable is True
    assert report.ok is False
    assert any("sanctum user exists" in i.name and not i.ok for i in report.items)


def test_status_command_cli_only(runner, monkeypatch):
    monkeypatch.setattr(su, "haus_tier_present", lambda: False)
    result = runner.invoke(app, ["service-user", "status"])
    assert result.exit_code == 0
    assert "n/a" in result.output or "not expected" in result.output


def test_check_exits_one_when_unhealthy(runner, monkeypatch):
    bad = su.Wave1Report(
        applicable=True,
        items=[su.CheckItem("sanctum user exists", False, "missing")],
    )
    monkeypatch.setattr(su, "check_wave1", lambda **k: bad)
    result = runner.invoke(app, ["service-user", "check"])
    assert result.exit_code == 1


def test_check_exits_zero_when_ok(runner, monkeypatch):
    good = su.Wave1Report(
        applicable=True,
        items=[su.CheckItem("all", True, "ok")],
    )
    monkeypatch.setattr(su, "check_wave1", lambda **k: good)
    result = runner.invoke(app, ["service-user", "check"])
    assert result.exit_code == 0


def test_install_dry_run(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(su, "operator_home", lambda: tmp_path)
    monkeypatch.setattr(
        su,
        "_package_asset",
        lambda name: f"<plist>@OPERATOR_HOME@/{name}</plist>",
    )
    result = runner.invoke(app, ["service-user", "install", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert not (tmp_path / ".sanctum/launchdaemons").exists()
    assert not (tmp_path / ".sanctum/scripts/service-user/README.md").exists()


def test_materialize_expands_operator_home(tmp_path, monkeypatch):
    monkeypatch.setattr(
        su,
        "_package_asset",
        lambda name: "HOME=@OPERATOR_HOME@\n",
    )
    dest = su.materialize_assets(tmp_path)
    text = (dest / "haus.sanctum.proxyd.plist").read_text()
    assert str(tmp_path) in text
    assert "@OPERATOR_HOME@" not in text


def test_onboard_operator_lists_service_user_gate():
    from sanctum_cli.commands import onboard

    assert "service-user-install" in onboard.RECIPE_GATES["operator"]
    assert "service-user-install" not in onboard.RECIPE_GATES["family"]
    assert "service-user-install" in onboard._CHAPTER_GATES["Your Network"]
    assert "service-user-install" in onboard._GATE_LABELS


def test_memory_vault_template_listens_on_loopback_only():
    import plistlib

    raw = su._package_asset("haus.sanctum.memory-vault.plist")
    pl = plistlib.loads(raw.encode())
    assert pl["EnvironmentVariables"]["MEMORY_VAULT_HOST"] == "127.0.0.1"


def test_run_install_uses_sudo_without_dash_e_and_python_dash_i(monkeypatch):
    import sys

    seen: list[list[str]] = []
    monkeypatch.setattr(su.os, "geteuid", lambda: 501)
    monkeypatch.setattr(su.subprocess, "call", lambda cmd: seen.append(list(cmd)) or 0)
    assert su.run_install(dry_run=False) == 0
    assert len(seen) == 1
    cmd = seen[0]
    assert cmd[0] == "sudo"
    assert "-E" not in cmd
    assert "--" in cmd
    assert cmd[cmd.index("--") + 1] == sys.executable
    assert "-I" in cmd
    # Through the module entry point, whose refusals print error:/fix:, not a traceback.
    assert cmd[-3:] == ["-m", "sanctum_cli.service_user", "install"]


def test_ensure_group_and_user_uses_locked_password_not_argv_token(monkeypatch, tmp_path):
    cmds: list[list[str]] = []
    monkeypatch.setattr(su.os, "geteuid", lambda: 0)
    monkeypatch.setattr(su, "service_user_exists", lambda name=su.SERVICE_USERNAME: False)

    def fake_run(cmd: list[str], **_kw):
        cmds.append(list(cmd))
        return su.subprocess.CompletedProcess(cmd, returncode=1 if cmd[0] == "sysadminctl" else 0, stdout="", stderr="")

    monkeypatch.setattr(su, "_run", fake_run)
    monkeypatch.setattr(su.Path, "mkdir", lambda *a, **k: None)
    monkeypatch.setattr(su.pwd, "getpwnam", lambda _n: (_ for _ in ()).throw(KeyError("none")))
    su._ensure_group_and_user()

    assert not any("-passwd" in c for c in cmds)
    assert ["dscl", ".", "-create", f"/Users/{su.SERVICE_USERNAME}", "Password", "*"] in cmds


def test_acl_operator_tree_never_recurses_on_sanctum_or_secrets(monkeypatch, tmp_path):
    sanctum = tmp_path / ".sanctum"
    for d in (
        "bin",
        "state",
        "secrets",
        "credentials",
        "private-state",
        "certs/clients",
    ):
        (sanctum / d).mkdir(parents=True, exist_ok=True)
    (sanctum / "secrets" / "command-center-session.key").write_text("secret", encoding="utf-8")
    (sanctum / "secrets" / "ha-token").write_text("svc-tok", encoding="utf-8")
    (sanctum / "certs" / "ca.key").write_text("ca-key", encoding="utf-8")
    (sanctum / "certs" / "ca.crt").write_text("ca-crt", encoding="utf-8")

    cmds: list[list[str]] = []
    monkeypatch.setattr(su, "_run", lambda cmd, **_kw: cmds.append(list(cmd)) or su.subprocess.CompletedProcess(cmd, 0, "", ""))
    su._acl_operator_tree(tmp_path)

    for c in cmds:
        if "-R" in c:
            target = c[-1]
            assert target != str(sanctum)
            assert "secrets" not in target
            assert "credentials" not in target
            assert "private-state" not in target
            assert "certs" not in target

    touched_files = [c[-1] for c in cmds]
    assert not any("command-center-session.key" in p for p in touched_files)
    assert not any("ca.key" in p for p in touched_files)
    assert any(p.endswith("ha-token") for p in touched_files)
    assert any(p.endswith("ca.crt") for p in touched_files)
