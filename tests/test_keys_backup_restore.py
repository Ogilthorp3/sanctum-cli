"""Round-trip test for `keys backup` / `keys restore` (deep-audit C7).

Mocks only the macOS `security` CLI with an in-memory (account, service) dict;
openssl + tar run for real, so this proves the encrypted bundle actually
decrypts and restores — including the restic passphrase that was missing.
"""
from __future__ import annotations

import subprocess

import pytest

from sanctum_cli.commands import keys_backup as kb


class _FakeKeychain:
    """In-memory stand-in for `security` keyed by (account, service)."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}
        self._real_run = subprocess.run  # capture before the monkeypatch

    def run(self, cmd, **kwargs):
        if cmd and cmd[0] == "security":
            account = cmd[cmd.index("-a") + 1]
            service = cmd[cmd.index("-s") + 1]
            if cmd[1] == "find-generic-password":
                val = self.store.get((account, service))
                if val is None:
                    return subprocess.CompletedProcess(cmd, 1, "", "not found")
                return subprocess.CompletedProcess(cmd, 0, val + "\n", "")
            if cmd[1] == "add-generic-password":
                self.store[(account, service)] = cmd[cmd.index("-w") + 1]
                return subprocess.CompletedProcess(cmd, 0, "", "")
        return self._real_run(cmd, **kwargs)  # openssl / tar for real


def test_keys_backup_restore_round_trip_includes_restic_passphrase(tmp_path, monkeypatch):
    fake = _FakeKeychain()
    fake.store[("sanctum-backup", "sanctum-backup-key")] = "the-restic-passphrase"
    fake.store[("sanctum", "google-ai-api-key")] = "gemini-secret"
    fake.store[("sanctum", "b2-account-id")] = "b2-id-secret"

    monkeypatch.setattr(kb.subprocess, "run", fake.run)
    monkeypatch.setattr(kb.getpass, "getpass", lambda *a, **k: "correct-horse-staple")

    bundle = tmp_path / "keys.tar.gz.enc"
    kb.keys_backup_command(out=bundle, yes=True)
    assert bundle.is_file()

    # Wipe the keychain, then restore from the bundle.
    fake.store.clear()
    kb.keys_restore_command(path=bundle)

    # The restic passphrase (the C7 gap) round-trips under its own account,
    # and the previously-drifted google/b2 names are present too.
    assert fake.store[("sanctum-backup", "sanctum-backup-key")] == "the-restic-passphrase"
    assert fake.store[("sanctum", "google-ai-api-key")] == "gemini-secret"
    assert fake.store[("sanctum", "b2-account-id")] == "b2-id-secret"


def test_keychain_services_names_match_the_real_write_sites():
    # Drift guard: the entries must match where the keys are actually stored.
    from sanctum_cli.backends import b2

    services = {s for (s, _a) in kb.KEYCHAIN_SERVICES}
    assert b2.KEYCHAIN_SERVICE_RESTIC in services  # sanctum-backup-key
    assert b2.KEYCHAIN_SERVICE_KEY_ID in services  # b2-account-id (not b2-application-key-id)
    assert "google-ai-api-key" in services         # not the never-written gemini-api-key
    assert "gemini-api-key" not in services
    assert "b2-application-key-id" not in services
    # The restic passphrase must be paired with its own account.
    restic = dict(kb.KEYCHAIN_SERVICES)[b2.KEYCHAIN_SERVICE_RESTIC]
    assert restic == b2.KEYCHAIN_ACCOUNT_RESTIC  # sanctum-backup
