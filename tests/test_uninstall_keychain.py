"""Uninstall Keychain-revoke correctness (deep-audit M9)."""
from __future__ import annotations

from sanctum_cli.backends import b2
from sanctum_cli.commands import uninstall


def test_revoke_list_matches_real_write_sites_and_preserves_restic():
    services = {s for (s, _a) in uninstall.KEYCHAIN_SERVICES}
    # Drift fixed: the keys that used to SURVIVE uninstall are now targeted.
    assert "google-ai-api-key" in services and "gemini-api-key" not in services
    assert b2.KEYCHAIN_SERVICE_KEY_ID in services  # b2-account-id
    assert "b2-application-key-id" not in services
    # The restic passphrase must be PRESERVED (decrypts the backups uninstall keeps).
    assert b2.KEYCHAIN_SERVICE_RESTIC not in services  # sanctum-backup-key
    # Device-admin entries live under the 'admin' account, not 'sanctum'.
    pairs = dict(uninstall.KEYCHAIN_SERVICES)
    assert pairs["bell-hub-admin"] == "admin"
    assert pairs["orbi-admin"] == "admin"


def test_revoke_uses_the_paired_account(monkeypatch):
    calls = []
    monkeypatch.setattr(
        uninstall, "revoke_keychain_entry", lambda account, service: calls.append((account, service)) or True
    )
    uninstall._revoke_keychain_entries()
    # Every call uses the account paired with the service (not a hardcoded 'sanctum').
    by_service = {service: account for (account, service) in calls}
    assert by_service["google-ai-api-key"] == "sanctum"
    assert by_service["bell-hub-admin"] == "admin"
    assert "sanctum-backup-key" not in by_service  # never revoked


def test_untap_homebrew_uninstalls_formula_before_untap(monkeypatch):
    import subprocess

    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "sanctum-cli 0.15.5\n", "")

    monkeypatch.setattr(uninstall.shutil, "which", lambda _name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(uninstall.subprocess, "run", fake_run)

    assert uninstall._untap_homebrew() is True
    assert calls == [
        ["brew", "list", "--versions", "ogilthorp3/sanctum/sanctum-cli"],
        ["brew", "uninstall", "ogilthorp3/sanctum/sanctum-cli"],
        ["brew", "untap", "ogilthorp3/sanctum"],
    ]


def test_untap_homebrew_skips_uninstall_when_not_installed_and_handles_missing_brew(monkeypatch):
    import subprocess

    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["brew", "list"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(uninstall.shutil, "which", lambda _name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(uninstall.subprocess, "run", fake_run)
    assert uninstall._untap_homebrew() is True
    assert calls == [
        ["brew", "list", "--versions", "ogilthorp3/sanctum/sanctum-cli"],
        ["brew", "untap", "ogilthorp3/sanctum"],
    ]

    monkeypatch.setattr(uninstall.shutil, "which", lambda _name: None)
    assert uninstall._untap_homebrew() is False


def test_uninstall_cli_dry_run_mentions_uninstall_and_untap(monkeypatch):
    from typer.testing import CliRunner

    from sanctum_cli.cli import app

    called = False

    def fail_run(*_a, **_kw):
        nonlocal called
        called = True
        raise AssertionError("dry-run must not run subprocesses")

    monkeypatch.setattr(uninstall.subprocess, "run", fail_run)
    res = CliRunner().invoke(app, ["uninstall", "--dry-run", "--yes"])
    assert res.exit_code == 0, res.output
    assert "uninstall sanctum-cli" in res.output
    assert "untap ogilthorp3/sanctum" in res.output
    assert called is False
