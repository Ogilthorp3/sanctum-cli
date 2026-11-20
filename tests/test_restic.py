"""Unit tests for shared restic environment builder (CRIT-02)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sanctum_cli import config
from sanctum_cli.keychain import KeychainEntryMissingError
from sanctum_cli.restic import restic_env_for_repo


def _repo(repo: str, *, account: str = "acct", service: str = "svc") -> config.CloudBackupRepo:
    return config.CloudBackupRepo(
        kind="restic",
        repo=repo,
        keychain=config.KeychainRef(account=account, service=service),
    )


def test_restic_env_for_repo_b2() -> None:
    def fake_read(*, account: str, service: str) -> str:
        return f"{account}/{service}"

    with patch("sanctum_cli.keychain.read", side_effect=fake_read):
        env = restic_env_for_repo(_repo("b2:my-bucket:sanctum"), base_env={"PATH": "/usr/bin"})

    assert env["RESTIC_PASSWORD"] == "acct/svc"
    assert env["B2_ACCOUNT_ID"] == "sanctum/b2-account-id"
    assert env["B2_ACCOUNT_KEY"] == "sanctum/b2-application-key"
    assert "AWS_ACCESS_KEY_ID" not in env


def test_restic_env_for_repo_r2_and_s3() -> None:
    def fake_read(*, account: str, service: str) -> str:
        return f"{account}/{service}"

    with patch("sanctum_cli.keychain.read", side_effect=fake_read):
        for target in ("r2:bucket", "s3:https://acct.r2.cloudflarestorage.com/bucket"):
            env = restic_env_for_repo(_repo(target), base_env={})
            assert env["RESTIC_PASSWORD"] == "acct/svc"
            assert env["AWS_ACCESS_KEY_ID"] == "sanctum/r2-access-key-id"
            assert env["AWS_SECRET_ACCESS_KEY"] == "sanctum/r2-secret-access-key"
            assert env["AWS_DEFAULT_REGION"] == "auto"
            assert "B2_ACCOUNT_ID" not in env


def test_restic_env_for_repo_generic_s3_falls_back_to_base_env() -> None:
    def fake_read(*, account: str, service: str) -> str:
        if service == "svc":
            return "repo-pw"
        raise KeychainEntryMissingError("missing r2 cred")

    with patch("sanctum_cli.keychain.read", side_effect=fake_read):
        env = restic_env_for_repo(
            _repo("s3:s3.amazonaws.com/my-bucket"),
            base_env={
                "AWS_ACCESS_KEY_ID": "existing-key",
                "AWS_SECRET_ACCESS_KEY": "existing-secret",
            },
        )
    assert env["RESTIC_PASSWORD"] == "repo-pw"
    assert env["AWS_ACCESS_KEY_ID"] == "existing-key"
    assert env["AWS_SECRET_ACCESS_KEY"] == "existing-secret"

    with (
        patch("sanctum_cli.keychain.read", side_effect=fake_read),
        pytest.raises(KeychainEntryMissingError),
    ):
        restic_env_for_repo(
            _repo("s3:https://id.r2.cloudflarestorage.com/my-bucket"),
            base_env={"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"},
        )


def test_restic_env_for_repo_local_and_cache_dedup() -> None:
    cache: dict[tuple[str, str], str] = {}
    with patch("sanctum_cli.keychain.read", return_value="local-pw") as kc:
        env1 = restic_env_for_repo(_repo("/Volumes/T9/restic"), base_env={}, cache=cache)
        env2 = restic_env_for_repo(_repo("/Volumes/T9/restic"), base_env={}, cache=cache)

    kc.assert_called_once_with(account="acct", service="svc")
    assert env1["RESTIC_PASSWORD"] == "local-pw"
    assert env2["RESTIC_PASSWORD"] == "local-pw"


def test_restic_env_for_repo_partial_r2_keychain_is_atomic() -> None:
    def fake_read(*, account: str, service: str) -> str:
        if service == "svc":
            return "repo-pw"
        if service == "r2-access-key-id":
            return "r2-only-access-key"
        raise KeychainEntryMissingError("missing r2 secret")

    with patch("sanctum_cli.keychain.read", side_effect=fake_read):
        env = restic_env_for_repo(
            _repo("s3:s3.amazonaws.com/my-bucket"),
            base_env={
                "AWS_ACCESS_KEY_ID": "aws-key",
                "AWS_SECRET_ACCESS_KEY": "aws-secret",
            },
        )

    assert env["RESTIC_PASSWORD"] == "repo-pw"
    assert env["AWS_ACCESS_KEY_ID"] == "aws-key"
    assert env["AWS_SECRET_ACCESS_KEY"] == "aws-secret"
    assert "AWS_DEFAULT_REGION" not in env


def test_restic_env_for_repo_uppercase_r2_host_fails_closed() -> None:
    def fake_read(*, account: str, service: str) -> str:
        if service == "svc":
            return "repo-pw"
        raise KeychainEntryMissingError("missing r2 cred")

    with (
        patch("sanctum_cli.keychain.read", side_effect=fake_read),
        pytest.raises(KeychainEntryMissingError),
    ):
        restic_env_for_repo(
            _repo("s3:https://id.R2.CLOUDFLARESTORAGE.COM/my-bucket"),
            base_env={"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"},
        )
