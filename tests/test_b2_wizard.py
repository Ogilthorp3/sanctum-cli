"""Tests for the Backblaze B2 setup wizard.

All network and subprocess boundaries are mocked. The flow is driven via
``CliRunner.invoke(input=...)`` so the Rich prompts read from the mocked
stdin.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from sanctum_cli.backends import b2
from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()

VALID_KEY_ID = "0" * 25  # 25 hex chars
assert b2.B2_KEY_ID_RE.match(VALID_KEY_ID)
VALID_APP_KEY = "K003" + "abcdefghijABCDEFGHIJ0123456789abcdefghij1234"  # K003 + 44 chars
assert b2.B2_APP_KEY_RE.match(VALID_APP_KEY)


def _completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _make_b2_auth_response() -> dict[str, object]:
    return {
        "accountId": "deadbeef0011",
        "apiInfo": {"storageApi": {"apiUrl": "https://api123.backblazeb2.com"}},
        "authorizationToken": "tok-XYZ",
    }


def test_b2_key_id_regex() -> None:
    assert b2.B2_KEY_ID_RE.match("0" * 25)
    assert not b2.B2_KEY_ID_RE.match("0" * 24)
    assert not b2.B2_KEY_ID_RE.match("not hex zzzzzzzzzzzzzzzzzz")


def test_keychain_account_restic_pins_legacy_value() -> None:
    """Regression: the restic passphrase Keychain entry is at account
    'sanctum-backup', not 'sanctum'. Bash-era backup script created it that
    way; renaming would orphan every existing install. Live-discovered
    2026-04-27 — never let this drift again."""
    assert b2.KEYCHAIN_ACCOUNT_RESTIC == "sanctum-backup"
    assert b2.KEYCHAIN_SERVICE_RESTIC == "sanctum-backup-key"


def test_b2_app_key_regex() -> None:
    assert b2.B2_APP_KEY_RE.match("K003" + "a" * 50)
    assert not b2.B2_APP_KEY_RE.match("X003" + "a" * 50)
    assert not b2.B2_APP_KEY_RE.match("K003abc")  # too short


def test_authorize_translates_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(401, text="invalid", request=httpx.Request("GET", "https://x"))

    with patch("sanctum_cli.backends.b2.httpx.get", side_effect=fake_get):
        from sanctum_cli.errors import UserError

        try:
            b2._b2_authorize("k", "v")
        except UserError as exc:
            assert "401" in exc.message
        else:
            raise AssertionError("expected UserError")


def test_authorize_extracts_apiurl_and_account_id() -> None:
    def fake_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(
            200,
            json=_make_b2_auth_response(),
            request=httpx.Request("GET", "https://x"),
        )

    with patch("sanctum_cli.backends.b2.httpx.get", side_effect=fake_get):
        result = b2._b2_authorize("k", "v")
    assert result.account_id == "deadbeef0011"
    assert result.api_url == "https://api123.backblazeb2.com"


def test_create_bucket_idempotent_on_duplicate() -> None:
    auth = b2._B2AuthResult(account_id="acc", api_url="https://api.x")

    def fake_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(200, json=_make_b2_auth_response(), request=httpx.Request("GET", "https://x"))

    def fake_post(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(
            400,
            json={"code": "duplicate_bucket_name", "message": "already exists"},
            request=httpx.Request("POST", "https://x"),
        )

    with (
        patch("sanctum_cli.backends.b2.httpx.get", side_effect=fake_get),
        patch("sanctum_cli.backends.b2.httpx.post", side_effect=fake_post),
    ):
        # Should NOT raise — duplicate_bucket_name is treated as success
        b2._b2_create_bucket(auth, "existing-bucket", "k", "v")


def test_persist_to_instance_yaml_merges_and_keeps_bak(tmp_path: Path) -> None:
    target = tmp_path / "instance.yaml"
    target.write_text(
        "instance:\n"
        "  name: Test\n"
        "  slug: test\n"
        "services:\n"
        "  whatever:\n"
        "    port: 1\n",
        encoding="utf-8",
    )
    b2._persist_to_instance_yaml(
        target,
        bucket="sanctum-restic-test-1234",
        keychain_service_restic="sanctum-backup-key",
        keychain_account="sanctum",
    )
    import yaml as _yaml

    parsed = _yaml.safe_load(target.read_text())
    assert parsed["instance"]["name"] == "Test"
    assert parsed["services"]["whatever"]["port"] == 1
    assert parsed["cli"]["cloud_backup"]["primary"]["repo"] == "b2:sanctum-restic-test-1234"
    assert (
        parsed["cli"]["cloud_backup"]["primary"]["keychain"]["service"] == "sanctum-backup-key"
    )
    bak_files = list(tmp_path.glob("instance.yaml.bak.*"))
    assert len(bak_files) == 1


def test_full_wizard_happy_path(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end run with B2 API + restic + keychain + prompts all mocked."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))

    def fake_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(
            200, json=_make_b2_auth_response(), request=httpx.Request("GET", "https://x")
        )

    def fake_post(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(200, json={}, request=httpx.Request("POST", "https://x"))

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _completed()

    # Side-step Rich's password prompt (which needs a real TTY for echo control)
    # by stubbing the validated-prompt helper directly.
    prompts: list[str] = []

    def fake_prompt(label, *, validator, hint, password=False, max_attempts=3):  # type: ignore[no-untyped-def]
        prompts.append(label)
        return VALID_KEY_ID if "keyID" in label else VALID_APP_KEY

    with (
        patch("sanctum_cli.backends.b2.httpx.get", side_effect=fake_get),
        patch("sanctum_cli.backends.b2.httpx.post", side_effect=fake_post),
        patch("sanctum_cli.backends.b2.subprocess.run", side_effect=fake_run),
        patch("sanctum_cli.backends.b2.shutil.which", return_value="/x"),
        patch("sanctum_cli.backends.b2.keychain.exists", return_value=True),
        patch("sanctum_cli.backends.b2.keychain.read", return_value="existing-pass"),
        patch("sanctum_cli.backends.b2._round_trip", return_value=None),
        patch("sanctum_cli.backends.b2.webbrowser.open", return_value=True),
        patch("sanctum_cli.backends.b2._prompt_validated", side_effect=fake_prompt),
        patch("sanctum_cli.backends.b2.Confirm.ask", return_value=True),
    ):
        result = runner.invoke(
            app, ["cloud", "setup", "--backend", "b2", "--no-open", "--no-persist"]
        )

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "done" in result.stdout.lower() or "cloud_backup" in result.stdout.lower()
    assert any("keyID" in p for p in prompts)
    assert any("applicationKey" in p for p in prompts)


def test_wizard_rejects_when_already_configured(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If cloud_backup.primary already set, refuse politely."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    result = runner.invoke(app, ["cloud", "setup", "--no-open", "--no-persist"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "already configured" in combined.lower()


def test_wizard_unknown_backend_rejected(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["cloud", "setup", "--backend", "dropbox"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "unknown backend" in combined.lower()


def test_wizard_gdrive_dispatches_to_gdrive_module(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As of v0.4, gdrive backend has its own wizard; verify cloud cmd dispatches."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    with patch("sanctum_cli.commands.cloud.gdrive.run_wizard") as mocked:
        runner.invoke(app, ["cloud", "setup", "--backend", "gdrive", "--no-open", "--no-persist"])
    mocked.assert_called_once()
