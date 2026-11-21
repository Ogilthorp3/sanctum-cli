"""Unit tests for ``sanctum vault-armor`` CLI wrapper."""

from __future__ import annotations

import errno
import json
import os
import stat
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()
_SUBCOMMANDS = [["status"], ["self-test"], ["unlock"], ["lock"]]


def _write_fake_armor_bin(tmp_path: Path) -> Path:
    fake = tmp_path / "sanctum-vault-armor"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "if cmd == 'self-test':\n"
        "    print(json.dumps({'ok': True, 'suite': 'sanctum-vault-armor'}))\n"
        "elif cmd == 'status':\n"
        "    probes = [{'index': 1, 'label': 'lan', 'reachable_and_dleq_valid': True}] if '--probe-witnesses' in sys.argv else []\n"
        "    print(json.dumps({\n"
        "        'vault_id': 'home-vault',\n"
        "        'mode': 'phase1-shadow-canary',\n"
        "        'threshold': '2-of-3',\n"
        "        'post_quantum': 'ML-KEM-768 + Ed25519-TOPRF-DLEQ + AES-256-GCM',\n"
        "        'volume': {'volume_uuid': 'UUID-1', 'mounted': True, 'locked': False, 'mount_point': '/Volumes/SanctumVault'},\n"
        "        'op_breakglass_vault': 'Sanctum',\n"
        "        'op_breakglass_item_id': 'item123',\n"
        "        'witness_probes': probes,\n"
        "    }))\n"
        "elif cmd == 'unlock':\n"
        "    print(json.dumps({'volume_name': 'SanctumVault', 'mount_point': '/Volumes/SanctumVault', 'last_action': 'unlocked'}))\n"
        "elif cmd == 'lock':\n"
        "    print(json.dumps({'volume_name': 'SanctumVault', 'volume_uuid': 'UUID-1'}))\n"
        "else:\n"
        "    sys.exit(2)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def test_vault_armor_self_test_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _write_fake_armor_bin(tmp_path)
    monkeypatch.setenv("SANCTUM_VAULT_ARMOR_BIN", str(fake))
    res = runner.invoke(app, ["vault-armor", "self-test", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["ok"] is True


def test_vault_armor_status_with_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _write_fake_armor_bin(tmp_path)
    monkeypatch.setenv("SANCTUM_VAULT_ARMOR_BIN", str(fake))
    res = runner.invoke(app, ["vault-armor", "status", "--probe-witnesses"])
    assert res.exit_code == 0, res.output
    assert "UNLOCKED & MOUNTED" in res.output
    assert "DLEQ OK" in res.output


def test_vault_armor_unlock_and_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _write_fake_armor_bin(tmp_path)
    monkeypatch.setenv("SANCTUM_VAULT_ARMOR_BIN", str(fake))
    u = runner.invoke(app, ["vault-armor", "unlock", "--wait", "5"])
    assert u.exit_code == 0, u.output
    assert "UNLOCKED" in u.output
    lk = runner.invoke(app, ["vault-armor", "lock"])
    assert lk.exit_code == 0, lk.output
    assert "LOCKED" in lk.output


def test_vault_armor_missing_binary_reports_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_VAULT_ARMOR_BIN", str(tmp_path / "missing-bin"))
    res = runner.invoke(app, ["vault-armor", "status"])
    assert res.exit_code != 0
    normalized = " ".join(res.output.split())
    assert "cargo build --release -p sanctum-vault-armor" in normalized


@pytest.mark.parametrize("command", _SUBCOMMANDS, ids=lambda c: c[0])
def test_vault_armor_binary_without_execute_permission_is_a_clean_error(
    command: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """subprocess raised PermissionError for a mode-0644 binary: a traceback, not exit 4."""
    fake = _write_fake_armor_bin(tmp_path)
    fake.chmod(0o644)
    monkeypatch.setenv("SANCTUM_VAULT_ARMOR_BIN", str(fake))
    res = runner.invoke(app, ["vault-armor", *command])
    assert res.exit_code == 4, res.output
    assert isinstance(res.exception, SystemExit)  # a clean exit, not a traceback
    assert res.stderr.splitlines() == [
        f"error: cannot run sanctum-vault-armor at {fake}: Permission denied",
        f"fix: make it an executable file you can run (ls -l {fake}; chmod +x {fake})",
    ]


def test_vault_armor_binary_this_mac_cannot_run_is_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other OSError from subprocess (here an exec format error) is exit 4 too."""
    fake = tmp_path / "sanctum-vault-armor"
    fake.write_bytes(b"\x00\x01 not a program")
    fake.chmod(0o755)
    monkeypatch.setenv("SANCTUM_VAULT_ARMOR_BIN", str(fake))
    res = runner.invoke(app, ["vault-armor", "status"])
    assert res.exit_code == 4, res.output
    assert isinstance(res.exception, SystemExit)
    assert res.stderr.splitlines() == [
        f"error: cannot run sanctum-vault-armor at {fake}: {os.strerror(errno.ENOEXEC)}",
        "fix: rebuild and reinstall it from sanctum-rs: "
        "cargo build --release -p sanctum-vault-armor",
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any directory")
def test_vault_armor_binary_behind_a_directory_it_cannot_search_is_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Looking for the binary raised PermissionError there, before it was even run."""
    locked = tmp_path / "locked"
    locked.mkdir()
    fake = _write_fake_armor_bin(locked)
    monkeypatch.setenv("SANCTUM_VAULT_ARMOR_BIN", str(fake))
    locked.chmod(0o000)
    try:
        res = runner.invoke(app, ["vault-armor", "status"])
    finally:
        locked.chmod(0o755)
    assert res.exit_code == 4, res.output
    assert isinstance(res.exception, SystemExit)
    assert res.stderr.splitlines()[0] == (
        f"error: cannot run sanctum-vault-armor at {fake}: Permission denied"
    )
    assert res.stderr.splitlines()[1] == (
        f"fix: make {locked} searchable by you (ls -ld {locked}; chmod u+x {locked})"
    )
