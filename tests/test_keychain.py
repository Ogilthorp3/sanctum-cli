"""Keychain wrapper tests — boundaries are mocked subprocess.run."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from sanctum_cli import keychain
from sanctum_cli.errors import LocalError
from sanctum_cli.keychain import KeychainEntryMissingError, KeychainLockedError


def _completed(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


@patch("sanctum_cli.keychain.shutil.which", return_value="/usr/bin/security")
@patch("sanctum_cli.keychain.subprocess.run")
def test_read_success(run_mock, which_mock):  # type: ignore[no-untyped-def]
    run_mock.return_value = _completed(0, stdout="hunter2\n")
    out = keychain.read("acct", "svc")
    assert out == "hunter2"
    assert which_mock.called


@patch("sanctum_cli.keychain.shutil.which", return_value="/usr/bin/security")
@patch("sanctum_cli.keychain.subprocess.run")
def test_missing_entry_maps_to_specific_error(run_mock, _which_mock):  # type: ignore[no-untyped-def]
    run_mock.return_value = _completed(
        44, stderr="The specified item could not be found in the keychain."
    )
    with pytest.raises(KeychainEntryMissingError) as ei:
        keychain.read("acct", "svc")
    assert "Keychain entry missing" in ei.value.message
    assert ei.value.fix is not None
    assert "add-generic-password" in ei.value.fix


@patch("sanctum_cli.keychain.shutil.which", return_value="/usr/bin/security")
@patch("sanctum_cli.keychain.subprocess.run")
def test_locked_keychain_maps_to_specific_error(run_mock, _which_mock):  # type: ignore[no-untyped-def]
    run_mock.return_value = _completed(36, stderr="errSecAuthFailed")
    with pytest.raises(KeychainLockedError) as ei:
        keychain.read("acct", "svc")
    assert "locked" in ei.value.message.lower()
    assert ei.value.fix is not None
    assert "unlock-keychain" in ei.value.fix


@patch("sanctum_cli.keychain.shutil.which", return_value="/usr/bin/security")
@patch("sanctum_cli.keychain.subprocess.run")
def test_other_failure_is_local_error(run_mock, _which_mock):  # type: ignore[no-untyped-def]
    run_mock.return_value = _completed(99, stderr="some new mode")
    with pytest.raises(LocalError) as ei:
        keychain.read("acct", "svc")
    assert "rc=99" in ei.value.message


@patch("sanctum_cli.keychain.shutil.which", return_value=None)
def test_missing_security_bin(_which_mock):  # type: ignore[no-untyped-def]
    with pytest.raises(LocalError) as ei:
        keychain.read("acct", "svc")
    assert "missing required binary" in ei.value.message
    assert ei.value.fix is not None


@patch("sanctum_cli.keychain.shutil.which", return_value="/usr/bin/security")
@patch("sanctum_cli.keychain.subprocess.run")
def test_exists_returns_true_on_success(run_mock, _which_mock):  # type: ignore[no-untyped-def]
    run_mock.return_value = _completed(0, stdout="x\n")
    assert keychain.exists("a", "b") is True


@patch("sanctum_cli.keychain.shutil.which", return_value="/usr/bin/security")
@patch("sanctum_cli.keychain.subprocess.run")
def test_exists_returns_false_on_missing(run_mock, _which_mock):  # type: ignore[no-untyped-def]
    run_mock.return_value = _completed(44)
    assert keychain.exists("a", "b") is False


@patch("sanctum_cli.keychain.shutil.which", return_value="/usr/bin/security")
@patch("sanctum_cli.keychain.subprocess.run")
def test_timeout_maps_to_local_error(run_mock, _which_mock):  # type: ignore[no-untyped-def]
    run_mock.side_effect = subprocess.TimeoutExpired(cmd="security", timeout=5)
    with pytest.raises(LocalError) as ei:
        keychain.read("a", "b")
    assert "timed out" in ei.value.message
