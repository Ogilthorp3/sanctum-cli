"""Shared restic subprocess environment builder (`CRIT-02`).

Injects ``RESTIC_PASSWORD`` from the repo's Keychain ref plus cloud backend
credentials (``B2_ACCOUNT_ID``/``B2_ACCOUNT_KEY`` for ``b2:``;
``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY``/``AWS_DEFAULT_REGION="auto"``
for ``r2:`` and ``s3:``) consistently across ``backup``, ``onboard``,
``status``, and ``doctor``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from sanctum_cli import keychain
from sanctum_cli.backends import b2 as _b2
from sanctum_cli.backends import r2 as _r2
from sanctum_cli.keychain import KeychainEntryMissingError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sanctum_cli import config


def _read_cached(
    account: str,
    service: str,
    cache: dict[tuple[str, str], str] | None = None,
) -> str:
    key = (account, service)
    if cache is not None and key in cache:
        return cache[key]
    value = keychain.read(account=account, service=service)
    if cache is not None:
        cache[key] = value
    return value


def restic_env_for_repo_path(
    repo: str,
    *,
    password: str,
    base_env: Mapping[str, str] | None = None,
    cache: dict[tuple[str, str], str] | None = None,
) -> dict[str, str]:
    """Build restic env from an explicit ``repo`` string and ``password``."""
    env = dict(os.environ if base_env is None else base_env)
    env["RESTIC_PASSWORD"] = password
    if repo.startswith("b2:"):
        env["B2_ACCOUNT_ID"] = _read_cached(
            _b2.KEYCHAIN_ACCOUNT, _b2.KEYCHAIN_SERVICE_KEY_ID, cache
        )
        env["B2_ACCOUNT_KEY"] = _read_cached(
            _b2.KEYCHAIN_ACCOUNT, _b2.KEYCHAIN_SERVICE_APP_KEY, cache
        )
    elif repo.startswith(("s3:", "r2:")):
        try:
            env["AWS_ACCESS_KEY_ID"] = _read_cached(
                _b2.KEYCHAIN_ACCOUNT, _r2.KEYCHAIN_SERVICE_R2_ACCESS_KEY, cache
            )
            env["AWS_SECRET_ACCESS_KEY"] = _read_cached(
                _b2.KEYCHAIN_ACCOUNT, _r2.KEYCHAIN_SERVICE_R2_SECRET, cache
            )
            env["AWS_DEFAULT_REGION"] = "auto"
        except KeychainEntryMissingError:
            # Generic non-R2 s3: repos can rely on caller-supplied AWS_* env vars.
            if (
                repo.startswith("s3:")
                and "r2.cloudflarestorage.com" not in repo
                and env.get("AWS_ACCESS_KEY_ID")
                and env.get("AWS_SECRET_ACCESS_KEY")
            ):
                pass
            else:
                raise
    return env


def restic_env_for_repo(
    repo_cfg: config.CloudBackupRepo,
    *,
    base_env: Mapping[str, str] | None = None,
    cache: dict[tuple[str, str], str] | None = None,
) -> dict[str, str]:
    """Build restic subprocess env for a configured :class:`CloudBackupRepo`."""
    password = _read_cached(repo_cfg.keychain.account, repo_cfg.keychain.service, cache)
    return restic_env_for_repo_path(
        repo_cfg.repo,
        password=password,
        base_env=base_env,
        cache=cache,
    )
