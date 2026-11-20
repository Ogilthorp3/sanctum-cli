"""Round-trip and security regression tests for ``keys backup`` / ``keys restore``.

Mocks only the macOS ``security`` CLI (via ``sanctum_cli.keychain.subprocess.run``)
with an in-memory ``(account, service)`` dict; v2 AES-256-GCM + PBKDF2-SHA256
(600k), in-memory tar assembly/extraction, and legacy v1 OpenSSL CBC decryption
run for real.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
from typing import TYPE_CHECKING, Any

import click
import pytest
from typer.testing import CliRunner

from sanctum_cli import keychain
from sanctum_cli.cli import app
from sanctum_cli.commands import keys_backup as kb

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


class _FakeKeychain:
    """In-memory stand-in for ``/usr/bin/security`` exercising ``keychain.read/write``."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.argv_history: list[list[str]] = []
        self._real_run = subprocess.run

    def run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[Any]:
        self.argv_history.append(list(cmd))
        if cmd == [keychain.SECURITY_BIN, "-i"]:
            stdin_line = str(kwargs.get("input", "")).strip()
            assert stdin_line.startswith("add-generic-password ")
            acct_m = re.search(r'-a "([^"]+)"', stdin_line)
            svc_m = re.search(r'-s "([^"]+)"', stdin_line)
            hex_m = re.search(r"-X ([0-9a-fA-F]+)", stdin_line)
            assert acct_m and svc_m
            acct = acct_m.group(1)
            svc = svc_m.group(1)
            val = bytes.fromhex(hex_m.group(1)).decode("utf-8") if hex_m else ""
            self.store[(acct, svc)] = val
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if len(cmd) >= 2 and cmd[0] == keychain.SECURITY_BIN and cmd[1] == "find-generic-password":
            account = cmd[cmd.index("-a") + 1]
            service = cmd[cmd.index("-s") + 1]
            val = self.store.get((account, service))
            if val is None:
                return subprocess.CompletedProcess(cmd, 44, "", "not found")
            return subprocess.CompletedProcess(cmd, 0, val + "\n", "")
        if cmd and cmd[0] == "openssl":
            return self._real_run(cmd, **kwargs)  # type: ignore[call-overload,no-any-return]
        raise AssertionError(f"unexpected subprocess cmd: {cmd!r}")


def _patch_fake_keychain(monkeypatch: pytest.MonkeyPatch) -> _FakeKeychain:
    fake = _FakeKeychain()
    monkeypatch.setattr(keychain.shutil, "which", lambda _b: keychain.SECURITY_BIN)
    monkeypatch.setattr(keychain.subprocess, "run", fake.run)
    return fake


def test_keys_backup_restore_round_trip_includes_restic_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _patch_fake_keychain(monkeypatch)
    fake.store[("sanctum-backup", "sanctum-backup-key")] = "the-restic-passphrase"
    fake.store[("sanctum", "google-ai-api-key")] = "gemini-secret"
    fake.store[("sanctum", "b2-account-id")] = "b2-id-secret"

    monkeypatch.setattr(kb.getpass, "getpass", lambda *a, **k: "correct-horse-staple")

    bundle = tmp_path / "keys.tar.gz.enc"
    kb.keys_backup_command(out=bundle, yes=True)
    assert bundle.is_file()
    assert (bundle.stat().st_mode & 0o777) == 0o600
    raw = bundle.read_bytes()
    assert raw.startswith(kb.MAGIC_V2)

    # Wipe the keychain, then restore from the bundle.
    fake.store.clear()
    kb.keys_restore_command(path=bundle)

    # Secrets never appeared on subprocess argv.
    for argv in fake.argv_history:
        joined = " ".join(argv)
        assert "the-restic-passphrase" not in joined
        assert "gemini-secret" not in joined
        assert "b2-id-secret" not in joined
        assert "-w" not in argv or argv[1] == "find-generic-password"

    assert fake.store[("sanctum-backup", "sanctum-backup-key")] == "the-restic-passphrase"
    assert fake.store[("sanctum", "google-ai-api-key")] == "gemini-secret"
    assert fake.store[("sanctum", "b2-account-id")] == "b2-id-secret"


def test_keys_restore_rejects_tampered_ciphertext_and_wrong_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _patch_fake_keychain(monkeypatch)
    fake.store[("sanctum", "google-ai-api-key")] = "gemini-secret"
    monkeypatch.setattr(kb.getpass, "getpass", lambda *a, **k: "correct-horse-staple")

    bundle = tmp_path / "keys.tar.gz.enc"
    kb.keys_backup_command(out=bundle, yes=True)

    # Wrong passphrase -> Exit 1
    fake.store.clear()
    monkeypatch.setattr(kb.getpass, "getpass", lambda *a, **k: "wrong-passphrase-123")
    with pytest.raises(click.exceptions.Exit) as exc:
        kb.keys_restore_command(path=bundle)
    assert exc.value.exit_code == 1
    assert fake.store == {}

    # Tampered ciphertext byte -> InvalidTag -> Exit 1
    tampered = bytearray(bundle.read_bytes())
    tampered[-1] ^= 0xFF
    bundle.write_bytes(bytes(tampered))
    monkeypatch.setattr(kb.getpass, "getpass", lambda *a, **k: "correct-horse-staple")
    with pytest.raises(click.exceptions.Exit) as exc2:
        kb.keys_restore_command(path=bundle)
    assert exc2.value.exit_code == 1
    assert fake.store == {}


def _build_malicious_v2_bundle(passphrase: str, build_tar: object) -> bytes:
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tf:
        build_tar(tf)  # type: ignore[operator]
    return kb._encrypt_v2(bio.getvalue(), passphrase)


def test_keys_restore_rejects_tar_traversal_symlink_and_bad_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _patch_fake_keychain(monkeypatch)
    passphrase = "correct-horse-staple"
    monkeypatch.setattr(kb.getpass, "getpass", lambda *a, **k: passphrase)

    def _add_file(tf: tarfile.TarFile, name: str, data: bytes) -> None:
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))

    # 1. Path traversal member
    b1 = tmp_path / "trav.enc"
    b1.write_bytes(
        _build_malicious_v2_bundle(
            passphrase, lambda tf: _add_file(tf, "sanctum-keys/../../evil", b"x")
        )
    )
    with pytest.raises(click.exceptions.Exit):
        kb.keys_restore_command(path=b1)

    # 2. Symlink member
    def _add_sym(tf: tarfile.TarFile) -> None:
        ti = tarfile.TarInfo("sanctum-keys/google-ai-api-key")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)

    b2 = tmp_path / "sym.enc"
    b2.write_bytes(_build_malicious_v2_bundle(passphrase, _add_sym))
    with pytest.raises(click.exceptions.Exit):
        kb.keys_restore_command(path=b2)

    # 3. Hardlink member
    def _add_lnk(tf: tarfile.TarFile) -> None:
        ti = tarfile.TarInfo("sanctum-keys/google-ai-api-key")
        ti.type = tarfile.LNKTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)

    b3 = tmp_path / "lnk.enc"
    b3.write_bytes(_build_malicious_v2_bundle(passphrase, _add_lnk))
    with pytest.raises(click.exceptions.Exit):
        kb.keys_restore_command(path=b3)

    # 4. Bad service name in MANIFEST.json
    def _add_bad_manifest(tf: tarfile.TarFile) -> None:
        manifest = {"services": [{"service": "../etc/passwd", "account": "sanctum"}]}
        _add_file(tf, "sanctum-keys/MANIFEST.json", json.dumps(manifest).encode("utf-8"))

    b4 = tmp_path / "manifest.enc"
    b4.write_bytes(_build_malicious_v2_bundle(passphrase, _add_bad_manifest))
    with pytest.raises(click.exceptions.Exit):
        kb.keys_restore_command(path=b4)

    assert fake.store == {}


@pytest.mark.parametrize(
    "dup_member",
    [
        "./sanctum-keys/openrouter-api-key",
        "sanctum-keys/./openrouter-api-key",
        "sanctum-keys//openrouter-api-key",
    ],
)
def test_keys_restore_rejects_uncanonical_duplicate_tar_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dup_member: str
) -> None:
    fake = _patch_fake_keychain(monkeypatch)
    passphrase = "correct-horse-staple"
    monkeypatch.setattr(kb.getpass, "getpass", lambda *a, **k: passphrase)

    manifest = json.dumps(
        {"services": [{"service": "openrouter-api-key", "account": "sanctum"}]}
    ).encode("utf-8")

    def _build_dup(tf: tarfile.TarFile) -> None:
        for name, data in (
            ("sanctum-keys/MANIFEST.json", manifest),
            ("sanctum-keys/openrouter-api-key", b"v1"),
            (dup_member, b"v2"),
        ):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))

    bundle = tmp_path / "dup.enc"
    bundle.write_bytes(_build_malicious_v2_bundle(passphrase, _build_dup))
    with pytest.raises(click.exceptions.Exit) as exc:
        kb.keys_restore_command(path=bundle)
    assert exc.value.exit_code == 1
    assert fake.store == {}


def test_keys_restore_legacy_v1_requires_flag_and_works_with_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _patch_fake_keychain(monkeypatch)
    passphrase = "legacy-passphrase-123"
    monkeypatch.setattr(kb.getpass, "getpass", lambda *a, **k: passphrase)

    # Build a genuine legacy OpenSSL Salted__ bundle in-memory via stdin/stdout.
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tf:
        manifest = {"services": [{"service": "google-ai-api-key", "account": "sanctum"}]}
        m_bytes = json.dumps(manifest).encode("utf-8")
        ti_m = tarfile.TarInfo("sanctum-keys/MANIFEST.json")
        ti_m.size = len(m_bytes)
        tf.addfile(ti_m, io.BytesIO(m_bytes))
        s_bytes = b"legacy-gemini-secret"
        ti_s = tarfile.TarInfo("sanctum-keys/google-ai-api-key")
        ti_s.size = len(s_bytes)
        tf.addfile(ti_s, io.BytesIO(s_bytes))

    r_fd, w_fd = os.pipe()
    os.write(w_fd, (passphrase + "\n").encode("utf-8"))
    os.close(w_fd)
    try:
        enc_proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"fd:{r_fd}"],
            input=bio.getvalue(),
            pass_fds=(r_fd,),
            capture_output=True,
            check=True,
        )
    finally:
        os.close(r_fd)

    legacy_bundle = tmp_path / "legacy.enc"
    legacy_bundle.write_bytes(enc_proc.stdout)
    assert legacy_bundle.read_bytes().startswith(b"Salted__")

    # Without --legacy -> refused cleanly
    res_refused = runner.invoke(app, ["keys", "restore", str(legacy_bundle)])
    assert res_refused.exit_code == 1
    assert "--legacy" in res_refused.output
    assert fake.store == {}

    # With --legacy -> succeeds and restores key
    res_ok = runner.invoke(app, ["keys", "restore", str(legacy_bundle), "--legacy"])
    assert res_ok.exit_code == 0, res_ok.output
    assert fake.store[("sanctum", "google-ai-api-key")] == "legacy-gemini-secret"


def test_keychain_services_names_match_the_real_write_sites() -> None:
    # Drift guard: the entries must match where the keys are actually stored.
    from sanctum_cli.backends import b2

    services = {s for (s, _a) in kb.KEYCHAIN_SERVICES}
    assert b2.KEYCHAIN_SERVICE_RESTIC in services  # sanctum-backup-key
    assert b2.KEYCHAIN_SERVICE_KEY_ID in services  # b2-account-id (not b2-application-key-id)
    assert "google-ai-api-key" in services  # not the never-written gemini-api-key
    assert "gemini-api-key" not in services
    assert "b2-application-key-id" not in services
    # The restic passphrase must be paired with its own account.
    restic = dict(kb.KEYCHAIN_SERVICES)[b2.KEYCHAIN_SERVICE_RESTIC]
    assert restic == b2.KEYCHAIN_ACCOUNT_RESTIC  # sanctum-backup
