"""Exit-code taxonomy and exception hierarchy.

The CLI maps every failure to a stable exit code so scripts can branch
without grepping output. Codes are documented in SPEC.md §4 and must
not be renumbered without a major version bump.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Stable contract for every ``sanctum`` invocation."""

    OK = 0
    USER_ERROR = 1
    PROVIDER_ERROR = 2
    NETWORK_ERROR = 3
    LOCAL_ERROR = 4
    CONFIG_ERROR = 5


class SanctumError(Exception):
    """Base exception. Concrete subclasses carry an ExitCode."""

    exit_code: ExitCode = ExitCode.LOCAL_ERROR

    def __init__(self, message: str, *, fix: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.fix = fix


class UserError(SanctumError):
    """Bad CLI input — missing arg, malformed flag value, etc."""

    exit_code = ExitCode.USER_ERROR


class ProviderError(SanctumError):
    """Provider rejected the request (rate limit, auth, model unavailable)."""

    exit_code = ExitCode.PROVIDER_ERROR


class NetworkError(SanctumError):
    """DNS/connection failure reaching a provider or backend."""

    exit_code = ExitCode.NETWORK_ERROR


class LocalError(SanctumError):
    """Local-system failure: Keychain locked, disk full, missing dependency."""

    exit_code = ExitCode.LOCAL_ERROR


class ConfigError(SanctumError):
    """``instance.yaml`` failed schema validation or could not be read."""

    exit_code = ExitCode.CONFIG_ERROR
