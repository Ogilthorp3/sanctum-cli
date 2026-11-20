"""macOS Keychain wrapper.

Credentials never live on disk in sanctum-cli; they are read from the
login Keychain via the ``security`` binary on every invocation. The
wrapper is the single place that shells out to ``security`` so the
boundary stays auditable.

Lock-state is reported transparently: if the Keychain is locked,
``read()`` raises ``KeychainLocked`` rather than blocking on a GUI
prompt — callers can surface this to the user with a fix suggestion.
"""

from __future__ import annotations

import shutil
import subprocess

from sanctum_cli.errors import LocalError

SECURITY_BIN = "/usr/bin/security"
TIMEOUT_S = 5


class KeychainLockedError(LocalError):
    """Keychain returned status 36 (auth required)."""


class KeychainEntryMissingError(LocalError):
    """Requested generic-password account/service tuple does not exist."""


def _ensure_security_bin() -> None:
    if not shutil.which(SECURITY_BIN):
        msg = f"missing required binary: {SECURITY_BIN}"
        raise LocalError(
            msg,
            fix="install Xcode Command Line Tools: xcode-select --install",
        )


def read(account: str, service: str) -> str:
    """Read a generic-password entry. Strips trailing newline.

    Raises ``KeychainEntryMissing`` if the (account, service) tuple is
    not present, ``KeychainLocked`` if the Keychain is locked, and
    ``LocalError`` for any other ``security`` failure.
    """
    _ensure_security_bin()
    try:
        result = subprocess.run(
            [SECURITY_BIN, "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = "security CLI timed out reading Keychain"
        raise LocalError(msg, fix="retry; if persistent, restart securityd") from exc

    if result.returncode == 0:
        return result.stdout.rstrip("\n")
    if result.returncode == 44:  # SecKeychainItem not found
        msg = f"Keychain entry missing: account={account!r}, service={service!r}"
        raise KeychainEntryMissingError(
            msg,
            fix=f'security add-generic-password -a "{account}" -s "{service}" -w',
        )
    if result.returncode == 36:  # errSecAuthFailed / locked
        msg = "Keychain is locked"
        raise KeychainLockedError(
            msg,
            fix='unlock with: security unlock-keychain "$HOME/Library/Keychains/login.keychain-db"',
        )
    stderr = result.stderr.strip() or "unknown error"
    msg = f"security CLI failed (rc={result.returncode}): {stderr}"
    raise LocalError(msg)


def _escape_security_interactive_arg(text: str) -> str:
    """Validate and quote an argument for ``security -i`` stdin commands."""
    if any(ch in text for ch in ("\r", "\n", "\0")):
        msg = "keychain account/service must not contain newlines or NUL"
        raise LocalError(msg)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write(account: str, service: str, value: str, *, update: bool = True) -> None:
    """Write a generic-password entry via ``security -i`` (never puts secret in argv)."""
    _ensure_security_bin()
    acct_tok = _escape_security_interactive_arg(account)
    svc_tok = _escape_security_interactive_arg(service)
    hex_val = value.encode("utf-8").hex()
    val_flag = f"-X {hex_val}" if hex_val else '-w ""'
    update_flag = " -U" if update else ""
    cmd_line = f"add-generic-password -a {acct_tok} -s {svc_tok} {val_flag}{update_flag}\n"
    try:
        result = subprocess.run(
            [SECURITY_BIN, "-i"],
            input=cmd_line,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = "security CLI timed out writing Keychain"
        raise LocalError(msg, fix="retry; if persistent, restart securityd") from exc

    if result.returncode == 0:
        return
    if result.returncode == 36:
        msg = "Keychain is locked"
        raise KeychainLockedError(
            msg,
            fix='unlock with: security unlock-keychain "$HOME/Library/Keychains/login.keychain-db"',
        )
    stderr = result.stderr.strip() or "unknown error"
    msg = f"Keychain write failed: {stderr}"
    raise LocalError(msg)


def exists(account: str, service: str) -> bool:
    """Return True iff the entry can be read. Never raises."""
    try:
        read(account, service)
    except (KeychainEntryMissingError, KeychainLockedError, LocalError):
        return False
    return True
