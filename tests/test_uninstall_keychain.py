"""Uninstall Keychain-revoke correctness (deep-audit M9)."""
from __future__ import annotations

from sanctum_cli.commands import uninstall
from sanctum_cli.backends import b2


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
