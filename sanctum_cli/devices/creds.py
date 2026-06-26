"""Headless device-credential resolution — Keychain → SOPS, NEVER 1Password.

The single seam every device provider's ``connect`` reads a password/token
through, with one strict resolution order (CLAUDE.md "Secrets" + the
headless-daemon constraint):

1. **macOS Keychain** (``security find-generic-password`` via
   :mod:`sanctum_cli.keychain`) — the GUI tier. A human-attended session can
   unlock it; when it holds the secret, that wins and nothing else is touched.
2. **SOPS** — decrypt the age-encrypted ``~/.sanctum/device-creds.enc.yaml`` and
   read ``devices.<service-slug>.password``. This is the FULLY HEADLESS tier: it
   needs only the age key on disk (``SOPS_AGE_KEY_FILE`` / the default keys.txt),
   so it works when the Keychain is locked — exactly the daemon case where no GUI
   exists to unlock it.
3. **NEVER** ``op`` / 1Password. Its unlock is a TouchID / biometric prompt that
   BLOCKS a headless daemon indefinitely, so the resolver is structurally
   incapable of shelling ``op`` — the only external binary it ever runs is
   ``sops`` (proven by ``tests/devices/test_device_creds_resolver.py``).

The keychain SERVICE (dash form, e.g. ``bell-hub-admin``) maps to the SOPS key
(underscore form, ``bell_hub_admin``) — the same slugging the trifecta uses, so a
secret seeded in one tier is addressable in the other under a stable name.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from sanctum_cli import keychain
from sanctum_cli.errors import LocalError

#: Env override → the default age-encrypted device-creds file. The default lives
#: under ``~/.sanctum`` (NOT this repo) so a friend-install with no SOPS file just
#: misses cleanly to the next tier.
ENV_DEVICE_CREDS_FILE = "SANCTUM_DEVICE_CREDS_FILE"
_DEFAULT_DEVICE_CREDS_FILE = Path("~/.sanctum/device-creds.enc.yaml").expanduser()

#: ``devices.<slug>`` field carrying the secret in the decrypted YAML.
_SOPS_SECRET_FIELD = "password"
_SOPS_DEVICES_KEY = "devices"

#: ``sops`` decrypt has no network and no biometric prompt (age-key only), so a
#: generous-but-bounded timeout is enough; it never hangs on a TouchID dialog.
_SOPS_TIMEOUT_S = 30


class DeviceCredError(LocalError):
    """No credential for an (account, service) in EITHER the Keychain or SOPS.

    A :class:`~sanctum_cli.errors.LocalError` subclass so it maps to the
    ``LOCAL_ERROR`` exit code and carries a ``fix=`` that points ONLY at the two
    sanctioned tiers (Keychain / SOPS) — never at 1Password/op.
    """


def _device_creds_path() -> Path:
    """The age-encrypted device-creds file (env override → default)."""
    override = os.environ.get(ENV_DEVICE_CREDS_FILE, "").strip()
    return Path(override).expanduser() if override else _DEFAULT_DEVICE_CREDS_FILE


def _sops_key(service: str) -> str:
    """Map a Keychain SERVICE (dash form) to its SOPS key (underscore form).

    ``bell-hub-admin`` → ``bell_hub_admin``; ``orbi-admin`` → ``orbi_admin``;
    ``firewalla-app`` → ``firewalla_app``. The same slugging the secrets trifecta
    uses, so a secret is addressable under a stable name across tiers. Any run of
    non-alphanumerics collapses to a single underscore (lower-cased), and edge
    underscores are trimmed.
    """
    return re.sub(r"[^a-z0-9]+", "_", service.lower()).strip("_")


def _run_sops(path: Path) -> str:
    """Decrypt ``path`` headlessly with ``sops`` and return the plaintext stdout.

    Shells ``sops --decrypt <path>`` — age-key only (NO ``op``, NO 1Password, NO
    network), so it cannot block on a biometric prompt. Raises
    :class:`DeviceCredError` when ``sops`` is not on PATH or the decrypt fails
    (a bad/absent age key, a malformed file) so the failure is legible rather
    than a silent empty read. This is the module's ONLY external-process seam —
    tests assert that the only binary it ever invokes is ``sops``.
    """
    sops_bin = shutil.which("sops")
    if sops_bin is None:
        msg = "sops is not on PATH — cannot read the headless device-creds fallback"
        raise DeviceCredError(
            msg,
            fix="install sops (brew install sops) or seed the secret in the macOS Keychain",
        )
    try:
        proc = subprocess.run(
            [sops_bin, "--decrypt", str(path)],
            capture_output=True,
            text=True,
            timeout=_SOPS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        msg = f"sops timed out decrypting {path}"
        raise DeviceCredError(msg, fix="check the age key (SOPS_AGE_KEY_FILE)") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "unknown error"
        msg = f"sops failed to decrypt {path}: {detail}"
        raise DeviceCredError(
            msg,
            fix=(
                "check the age key is present (SOPS_AGE_KEY_FILE or "
                "~/.config/sops/age/keys.txt) and the file is valid SOPS"
            ),
        )
    return proc.stdout


def _load_device_creds(path: Path | None = None) -> dict[str, Any]:
    """Decrypt + parse the device-creds YAML; ``{}`` when the file is absent.

    A missing file is a clean "no SOPS tier here" (returns ``{}``) — NOT an error,
    so a friend-install with no SOPS file falls through to the fail-closed
    DeviceCredError only after both tiers are genuinely exhausted. A present-but-
    undecryptable file raises (via :func:`_run_sops`), because that is a real
    misconfiguration the operator must see.
    """
    target = path if path is not None else _device_creds_path()
    if not target.exists():
        return {}
    data = yaml.safe_load(_run_sops(target))
    return data if isinstance(data, dict) else {}


def _sops_secret(service: str, *, path: Path | None = None) -> str | None:
    """Read ``devices.<sops_key(service)>.password`` from the SOPS file, or ``None``.

    ``None`` means "this tier does not have it" (file absent, or no row for this
    service) so the caller can fail-close legibly; a decrypt FAILURE propagates as
    :class:`DeviceCredError` (a present file that won't open is an operator error,
    not a silent miss).
    """
    data = _load_device_creds(path)
    devices = data.get(_SOPS_DEVICES_KEY)
    if not isinstance(devices, dict):
        return None
    entry = devices.get(_sops_key(service))
    if not isinstance(entry, dict):
        return None
    secret = entry.get(_SOPS_SECRET_FIELD)
    return None if secret is None else str(secret)


def resolve_secret(account: str, service: str) -> str:
    """Resolve a device secret: Keychain → SOPS → fail-closed. NEVER op/1Password.

    Tries the macOS Keychain first (under the caller's resolved ``(account,
    service)`` tuple); on ANY Keychain miss — entry absent, Keychain LOCKED (the
    headless case), or the ``security`` binary unavailable — falls back to the
    age-encrypted SOPS file's ``devices.<service-slug>.password``. Raises
    :class:`DeviceCredError` only when BOTH sanctioned tiers come up empty, with a
    fix that names only those two tiers (1Password is deliberately excluded — its
    biometric unlock blocks a headless daemon).
    """
    try:
        return keychain.read(account=account, service=service)
    except LocalError:
        # Every keychain.read failure (KeychainEntryMissing / KeychainLocked /
        # missing-binary) is a LocalError — fall through to the headless tier. A
        # locked Keychain is precisely when the age-key SOPS tier must answer.
        pass
    secret = _sops_secret(service)
    if secret is not None:
        return secret
    msg = f"no credential for service={service!r} account={account!r} in Keychain or SOPS"
    raise DeviceCredError(
        msg,
        fix=(
            f"seed it in the Keychain (security add-generic-password -a {account!r} "
            f"-s {service!r} -w <value>) or add devices.{_sops_key(service)}.password "
            "to ~/.sanctum/device-creds.enc.yaml"
        ),
    )


def resolve_secret_optional(account: str, service: str) -> str | None:
    """Best-effort :func:`resolve_secret` — ``None`` instead of raising on total miss.

    For callers that must fail-soft (a token whose absence simply means a transport
    is unavailable, e.g. the Firewalla bridge probe), so a missing credential never
    crashes a read path. A genuine SOPS decrypt error is ALSO swallowed to ``None``
    here — the caller's contract is "no token ⇒ no transport", not "surface the
    misconfiguration"; the strict :func:`resolve_secret` is used where the secret
    is mandatory.
    """
    try:
        return resolve_secret(account, service)
    except LocalError:
        return None


__all__ = [
    "ENV_DEVICE_CREDS_FILE",
    "DeviceCredError",
    "resolve_secret",
    "resolve_secret_optional",
]
