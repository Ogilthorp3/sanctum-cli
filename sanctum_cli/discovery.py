"""Discovery resolver — env > config > default.

Per the No-Hardcoded-Endpoints doctrine, every IP/port/hostname/path used
by sanctum-cli flows through ``resolve()``. Callers pass a key, the
config-derived value, and a default. Env overrides win, then config,
then default.

Env var names are derived as ``SANCTUM_<KEY>`` with dots → underscores
and uppercased: ``providers.claude.proxy_endpoint`` →
``SANCTUM_PROVIDERS_CLAUDE_PROXY_ENDPOINT``.
"""

from __future__ import annotations

import os
from typing import TypeVar

T = TypeVar("T")

ENV_PREFIX = "SANCTUM_"


def env_var_for(key: str) -> str:
    """``providers.claude.endpoint`` → ``SANCTUM_PROVIDERS_CLAUDE_ENDPOINT``."""
    sanitized = key.replace(".", "_").replace("-", "_").upper()
    return f"{ENV_PREFIX}{sanitized}"


def resolve(key: str, config_value: T | None, default: T) -> T | str:
    """Return env override if set, else config value, else default.

    Env values are always strings; callers responsible for typing if a
    non-string default is supplied. The function is a thin coordinator,
    not a parser — that boundary lives in the config layer.
    """
    env_value = os.environ.get(env_var_for(key))
    if env_value is not None:
        return env_value
    if config_value is not None:
        return config_value
    return default
