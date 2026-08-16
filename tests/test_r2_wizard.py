"""Tests for the Cloudflare R2 setup wizard.

SigV4 signing is exercised live (deterministic — pure crypto). Network +
subprocess + prompts are mocked. Verifies regex contracts, idempotent
bucket creation, full happy path, refusals.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from sanctum_cli.backends import r2
from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()

VALID_ACCOUNT_ID = "0" * 32
VALID_ACCESS_KEY = "1" * 32
VALID_SECRET_KEY = "2" * 64


def _completed(rc: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


# ─── Regex contracts ────────────────────────────────────────────────


def test_account_id_regex_pins_32_lowercase_hex() -> None:
    assert r2.R2_HEX32_RE.match("0" * 32)
    assert r2.R2_HEX32_RE.match("0123456789abcdef0123456789abcdef")
    assert not r2.R2_HEX32_RE.match("0" * 31)
    assert not r2.R2_HEX32_RE.match("0" * 33)
    assert not r2.R2_HEX32_RE.match("0123456789ABCDEF0123456789abcdef")  # uppercase


def test_secret_key_regex_pins_64_lowercase_hex() -> None:
    assert r2.R2_HEX64_RE.match("a" * 64)
    assert not r2.R2_HEX64_RE.match("a" * 63)
    assert not r2.R2_HEX64_RE.match("g" * 64)  # not hex


# ─── SigV4 invariants ───────────────────────────────────────────────


def test_sigv4_includes_required_headers() -> None:
    creds = r2._R2Creds(VALID_ACCOUNT_ID, VALID_ACCESS_KEY, VALID_SECRET_KEY)
    headers = r2._sigv4_sign(creds, "GET", "/")
    assert "authorization" in headers
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "Credential=" + VALID_ACCESS_KEY in headers["authorization"]
    assert "x-amz-content-sha256" in headers
    assert "x-amz-date" in headers
    assert "host" in headers
    assert headers["host"].endswith(".r2.cloudflarestorage.com")


def test_sigv4_signature_changes_with_method() -> None:
    creds = r2._R2Creds(VALID_ACCOUNT_ID, VALID_ACCESS_KEY, VALID_SECRET_KEY)
    h_get = r2._sigv4_sign(creds, "GET", "/")
    h_put = r2._sigv4_sign(creds, "PUT", "/some-bucket")
    assert h_get["authorization"] != h_put["authorization"]


# ─── R2 API ─────────────────────────────────────────────────────────


def test_list_buckets_403_translates_to_user_error() -> None:
    creds = r2._R2Creds(VALID_ACCOUNT_ID, VALID_ACCESS_KEY, VALID_SECRET_KEY)
    with patch(
        "sanctum_cli.backends.r2.httpx.get",
        return_value=httpx.Response(
            403, text="forbidden", request=httpx.Request("GET", "https://x")
        ),
    ):
        from sanctum_cli.errors import UserError

        try:
            r2._list_buckets(creds)
        except UserError as exc:
            assert "403" in exc.message
            assert exc.fix is not None
        else:
            raise AssertionError("expected UserError")


def test_list_buckets_parses_xml_names() -> None:
    creds = r2._R2Creds(VALID_ACCOUNT_ID, VALID_ACCESS_KEY, VALID_SECRET_KEY)
    body = (
        '<?xml version="1.0"?>'
        "<ListAllMyBucketsResult><Buckets>"
        "<Bucket><Name>alpha</Name></Bucket>"
        "<Bucket><Name>beta</Name></Bucket>"
        "</Buckets></ListAllMyBucketsResult>"
    )
    with patch(
        "sanctum_cli.backends.r2.httpx.get",
        return_value=httpx.Response(200, text=body, request=httpx.Request("GET", "https://x")),
    ):
        names = r2._list_buckets(creds)
    assert names == ["alpha", "beta"]


def test_create_bucket_idempotent_on_already_owned() -> None:
    creds = r2._R2Creds(VALID_ACCOUNT_ID, VALID_ACCESS_KEY, VALID_SECRET_KEY)
    body = '<?xml version="1.0"?><Error><Code>BucketAlreadyOwnedByYou</Code></Error>'
    with patch(
        "sanctum_cli.backends.r2.httpx.put",
        return_value=httpx.Response(409, text=body, request=httpx.Request("PUT", "https://x")),
    ):
        # Should NOT raise
        r2._create_bucket(creds, "existing-bucket")


def test_create_bucket_other_failure_raises() -> None:
    creds = r2._R2Creds(VALID_ACCOUNT_ID, VALID_ACCESS_KEY, VALID_SECRET_KEY)
    with patch(
        "sanctum_cli.backends.r2.httpx.put",
        return_value=httpx.Response(
            500, text="server error", request=httpx.Request("PUT", "https://x")
        ),
    ):
        from sanctum_cli.errors import UserError

        try:
            r2._create_bucket(creds, "bucket")
        except UserError as exc:
            assert "500" in exc.message
        else:
            raise AssertionError("expected UserError")


# ─── Persist ────────────────────────────────────────────────────────


def test_persist_to_instance_yaml_writes_repo_and_bak(tmp_path: Path) -> None:
    target = tmp_path / "instance.yaml"
    target.write_text(
        "instance:\n  name: T\n  slug: t\nservices:\n  x:\n    port: 1\n",
        encoding="utf-8",
    )
    repo = "s3:https://abc.r2.cloudflarestorage.com/sanctum-restic-host-1234"
    r2._persist(
        target,
        slot="primary",
        repo=repo,
        keychain_service_restic="sanctum-backup-key",
        keychain_account="sanctum-backup",
    )
    import yaml as _yaml

    parsed = _yaml.safe_load(target.read_text())
    assert parsed["services"]["x"]["port"] == 1
    assert parsed["cli"]["cloud_backup"]["primary"]["repo"] == repo
    assert parsed["cli"]["cloud_backup"]["primary"]["keychain"]["account"] == "sanctum-backup"
    bak_files = list(tmp_path.glob("instance.yaml.bak.*"))
    assert len(bak_files) == 1


def test_persist_writes_secondary_slot(tmp_path: Path) -> None:
    target = tmp_path / "instance.yaml"
    target.write_text(
        "instance:\n  name: T\n  slug: t\n"
        "cli:\n  cloud_backup:\n    primary:\n      kind: restic\n"
        "      repo: /Volumes/T9/sanctum-restic\n"
        "      keychain: { service: sanctum-backup-key, account: sanctum-backup }\n",
        encoding="utf-8",
    )
    repo = "s3:https://abc.r2.cloudflarestorage.com/another"
    r2._persist(
        target,
        slot="secondary",
        repo=repo,
        keychain_service_restic="sanctum-backup-key",
        keychain_account="sanctum-backup",
    )
    import yaml as _yaml

    parsed = _yaml.safe_load(target.read_text())
    # primary preserved, secondary added
    assert parsed["cli"]["cloud_backup"]["primary"]["repo"] == "/Volumes/T9/sanctum-restic"
    assert parsed["cli"]["cloud_backup"]["secondary"]["repo"] == repo


# ─── Full wizard happy path ─────────────────────────────────────────


def test_wizard_default_is_r2(minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`sanctum cloud setup` (no --backend) routes to R2."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    with patch("sanctum_cli.commands.cloud.r2.run_wizard") as mocked:
        runner.invoke(app, ["cloud", "setup", "--no-open", "--no-persist"])
    mocked.assert_called_once()


def test_full_wizard_happy_path(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))

    list_body = (
        '<?xml version="1.0"?><ListAllMyBucketsResult><Buckets></Buckets></ListAllMyBucketsResult>'
    )

    def fake_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(200, text=list_body, request=httpx.Request("GET", "https://x"))

    def fake_put(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return httpx.Response(200, text="", request=httpx.Request("PUT", "https://x"))

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _completed()

    def fake_prompt(label, *, validator, hint, password=False, max_attempts=3):  # type: ignore[no-untyped-def]
        if "account ID" in label:
            return VALID_ACCOUNT_ID
        if "access key" in label:
            return VALID_ACCESS_KEY
        return VALID_SECRET_KEY

    with (
        patch("sanctum_cli.backends.r2.httpx.get", side_effect=fake_get),
        patch("sanctum_cli.backends.r2.httpx.put", side_effect=fake_put),
        patch("sanctum_cli.backends.r2.subprocess.run", side_effect=fake_run),
        patch("sanctum_cli.backends.r2.shutil.which", return_value="/x"),
        patch("sanctum_cli.backends.r2._prompt_validated", side_effect=fake_prompt),
        patch("sanctum_cli.backends.r2.Confirm.ask", return_value=True),
        patch("sanctum_cli.backends.r2._round_trip", return_value=None),
        patch("sanctum_cli.backends.r2.webbrowser.open", return_value=True),
        patch("sanctum_cli.backends.r2.keychain.exists", return_value=True),
        patch("sanctum_cli.backends.r2.keychain.read", return_value="passphrase"),
        patch("sanctum_cli.backends.r2._ensure_keychain_entry", return_value=None),
    ):
        result = runner.invoke(app, ["cloud", "setup", "--no-open", "--no-persist"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "done" in result.stdout.lower() or "cloud_backup" in result.stdout.lower()
    assert "r2.cloudflarestorage.com" in result.stdout


def test_wizard_rejects_when_both_slots_full(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """full_instance_yaml has both primary + secondary configured."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    with patch("sanctum_cli.backends.r2.shutil.which", return_value="/x"):
        result = runner.invoke(app, ["cloud", "setup", "--no-open", "--no-persist"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "primary" in combined.lower() and "secondary" in combined.lower()


def test_wizard_unknown_backend_rejected(
    minimal_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(minimal_instance_yaml))
    result = runner.invoke(app, ["cloud", "setup", "--backend", "dropbox"])
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "unknown backend" in combined.lower()
    # Help text should advertise all three
    assert "r2" in combined or "b2" in combined
