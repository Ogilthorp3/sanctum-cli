"""Shared device-credential resolver — Keychain → SOPS, NEVER 1Password/op.

The contract under test (CLAUDE.md "Secrets" + the headless-daemon constraint):

  1. macOS Keychain (``security find-generic-password``) is tried FIRST — the GUI
     tier a human-attended session can unlock.
  2. When the Keychain misses (entry absent) OR is LOCKED (the headless-daemon
     case — no GUI to unlock it), fall back to decrypting the SOPS-encrypted
     ``~/.sanctum/device-creds.enc.yaml`` with the age key (fully headless).
  3. ``op``/1Password is NEVER consulted — its TouchID prompt blocks a headless
     daemon, so the resolver must be structurally incapable of shelling ``op``.

The keychain layer is monkeypatched (``sanctum_cli.keychain.read``, which the
resolver calls through) so no real prompt fires; the SOPS subprocess seam
(``creds._run_sops``) is monkeypatched for the precedence/derivation logic — and
ALSO exercised for real against an age-encrypted fixture (``sops``/``age``
present) so the decrypt boundary is proven, not just asserted structurally
(Contracts at the Boundary §3: don't mock a cheap subprocess boundary).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from sanctum_cli.devices import creds
from sanctum_cli.keychain import KeychainEntryMissingError, KeychainLockedError

if TYPE_CHECKING:
    from pathlib import Path

# A realistic ``sops -d`` stdout: the decrypted plaintext YAML the resolver parses.
# Keys are the underscored service slugs the haus seeds (CLAUDE.md TASK 6).
_DECRYPTED_YAML = """\
devices:
  bell_hub_admin:
    account: admin
    password: hub-sops-secret
  orbi_admin:
    account: admin
    password: orbi-sops-secret
  firewalla_app:
    account: bertrand@nepveu.name
    password: fw-sops-token
"""


def _kc_returns(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: value)


def _kc_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def _raise(account: str, service: str) -> str:
        raise exc

    monkeypatch.setattr("sanctum_cli.keychain.read", _raise)


def _sops_returns(monkeypatch: pytest.MonkeyPatch, yaml_text: str, creds_file: Path) -> None:
    """Point the resolver at an (existing) creds file and stub the sops decrypt."""
    creds_file.write_text("ENC", encoding="utf-8")  # existence gate only; decrypt is stubbed
    monkeypatch.setenv(creds.ENV_DEVICE_CREDS_FILE, str(creds_file))
    monkeypatch.setattr(creds, "_run_sops", lambda path: yaml_text)


def _sops_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(path: Path) -> str:
        msg = "SOPS must NOT be consulted when the Keychain already has the secret"
        raise AssertionError(msg)

    monkeypatch.setattr(creds, "_run_sops", _boom)


# ── precedence: Keychain wins, SOPS is the fallback ──────────────────────────


def test_resolver_prefers_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Keychain hit short-circuits — the SOPS seam is never touched."""
    _kc_returns(monkeypatch, "kc-secret")
    _sops_must_not_run(monkeypatch)
    assert creds.resolve_secret("admin", "bell-hub-admin") == "kc-secret"


def test_resolver_threads_account_and_service_to_keychain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The (account, service) the caller passes is exactly what reaches keychain.read."""
    seen: dict[str, str] = {}

    def _capture(account: str, service: str) -> str:
        seen["account"], seen["service"] = account, service
        return "kc-secret"

    monkeypatch.setattr("sanctum_cli.keychain.read", _capture)
    creds.resolve_secret("operator", "my-router-admin")
    assert seen == {"account": "operator", "service": "my-router-admin"}


def test_resolver_falls_back_to_sops_when_entry_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keychain entry absent → SOPS decrypt supplies the password."""
    _kc_raises(monkeypatch, KeychainEntryMissingError("no entry"))
    _sops_returns(monkeypatch, _DECRYPTED_YAML, tmp_path / "device-creds.enc.yaml")
    assert creds.resolve_secret("admin", "bell-hub-admin") == "hub-sops-secret"


def test_resolver_falls_back_to_sops_when_keychain_locked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A LOCKED Keychain (the headless-daemon case) → SOPS, not a blocking prompt."""
    _kc_raises(monkeypatch, KeychainLockedError("locked"))
    _sops_returns(monkeypatch, _DECRYPTED_YAML, tmp_path / "device-creds.enc.yaml")
    assert creds.resolve_secret("admin", "orbi-admin") == "orbi-sops-secret"


def test_resolver_sops_key_derivation_dash_to_underscore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The keychain SERVICE (dash form) maps to the SOPS key (underscore form)."""
    # bell-hub-admin → devices.bell_hub_admin ; orbi-admin → devices.orbi_admin ;
    # firewalla-app → devices.firewalla_app (the exact slugs the haus seeds).
    assert creds._sops_key("bell-hub-admin") == "bell_hub_admin"
    assert creds._sops_key("orbi-admin") == "orbi_admin"
    assert creds._sops_key("firewalla-app") == "firewalla_app"
    _kc_raises(monkeypatch, KeychainEntryMissingError("no entry"))
    _sops_returns(monkeypatch, _DECRYPTED_YAML, tmp_path / "device-creds.enc.yaml")
    assert creds.resolve_secret("bertrand@nepveu.name", "firewalla-app") == "fw-sops-token"


# ── fail-closed: both tiers miss ─────────────────────────────────────────────


def test_resolver_raises_when_both_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keychain miss + no SOPS file → a legible DeviceCredError, never None/op."""
    _kc_raises(monkeypatch, KeychainEntryMissingError("no entry"))
    monkeypatch.setenv(creds.ENV_DEVICE_CREDS_FILE, str(tmp_path / "absent.enc.yaml"))
    with pytest.raises(creds.DeviceCredError) as ei:
        creds.resolve_secret("admin", "orbi-admin")
    # The error must NOT point the user at 1Password/op (it is deliberately excluded).
    blob = f"{ei.value.message} {ei.value.fix or ''}".lower()
    assert "1password" not in blob
    assert "op " not in blob


def test_resolver_optional_returns_none_on_total_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """resolve_secret_optional swallows the miss to None (best-effort callers)."""
    _kc_raises(monkeypatch, KeychainEntryMissingError("no entry"))
    monkeypatch.setenv(creds.ENV_DEVICE_CREDS_FILE, str(tmp_path / "absent.enc.yaml"))
    assert creds.resolve_secret_optional("admin", "orbi-admin") is None


def test_resolver_sops_key_missing_falls_through_to_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A SOPS file present but WITHOUT this service's key → fail-closed, not a wrong row."""
    _kc_raises(monkeypatch, KeychainEntryMissingError("no entry"))
    _sops_returns(monkeypatch, _DECRYPTED_YAML, tmp_path / "device-creds.enc.yaml")
    with pytest.raises(creds.DeviceCredError):
        creds.resolve_secret("admin", "printer-admin")  # no devices.printer_admin key


# ── NEVER op/1Password ───────────────────────────────────────────────────────


def test_resolver_never_shells_out_to_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Across a full Keychain→SOPS fallback, the only external binary is ``sops``.

    Behavioral proof (not just "the string 'op' is absent"): every subprocess the
    resolver issues is recorded; the resolver must shell ``sops`` and NOTHING named
    ``op`` (1Password). A locked keychain forces the SOPS tier to actually run.
    """
    recorded: list[list[str]] = []

    def _spy_run(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="boom")

    _kc_raises(monkeypatch, KeychainLockedError("locked"))
    creds_file = tmp_path / "device-creds.enc.yaml"
    creds_file.write_text("ENC", encoding="utf-8")
    monkeypatch.setenv(creds.ENV_DEVICE_CREDS_FILE, str(creds_file))
    # Pin a sops binary so the seam runs deterministically on any platform.
    monkeypatch.setattr(creds.shutil, "which", lambda name: "/usr/bin/sops")
    monkeypatch.setattr(creds.subprocess, "run", _spy_run)

    with pytest.raises(creds.DeviceCredError):
        creds.resolve_secret("admin", "orbi-admin")

    assert recorded, "expected the SOPS subprocess seam to actually run on a locked keychain"
    for cmd in recorded:
        argv0 = cmd[0].rsplit("/", 1)[-1]
        assert argv0 != "op", f"resolver must never invoke 1Password's op: {cmd!r}"
        assert argv0 == "sops", f"the only external binary may be sops: {cmd!r}"


def test_run_sops_builds_decrypt_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_run_sops issues ``sops --decrypt <path>`` (headless, age-key) and returns stdout."""
    recorded: dict[str, object] = {}

    def _spy_run(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=_DECRYPTED_YAML, stderr="")

    monkeypatch.setattr(creds.shutil, "which", lambda name: "/usr/bin/sops")
    monkeypatch.setattr(creds.subprocess, "run", _spy_run)
    out = creds._run_sops(tmp_path / "device-creds.enc.yaml")
    assert out == _DECRYPTED_YAML
    cmd = recorded["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0].rsplit("/", 1)[-1] == "sops"
    assert "--decrypt" in cmd or "-d" in cmd
    assert str(tmp_path / "device-creds.enc.yaml") in cmd


# ── real sops/age roundtrip (skips cleanly when the binaries are absent) ──────


def test_resolve_secret_real_sops_age_roundtrip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end through the REAL sops binary: encrypt with age, decrypt headlessly.

    Proves the decrypt contract against the actual transport (not a stubbed
    stdout): an age keypair encrypts a fixture, then the resolver — with the
    Keychain forced to miss — decrypts it via ``sops`` using only the age key
    (no GUI, no op). Skips on a box without ``sops``/``age-keygen`` (e.g. Linux CI).
    """
    sops_bin = shutil.which("sops")
    age_keygen = shutil.which("age-keygen")
    if not sops_bin or not age_keygen:
        pytest.skip("sops/age-keygen not installed — real roundtrip is opt-in")

    key_file = tmp_path / "age.key"
    subprocess.run([age_keygen, "-o", str(key_file)], capture_output=True, text=True, check=True)
    recipient = ""
    for line in key_file.read_text(encoding="utf-8").splitlines():
        if "public key:" in line:
            recipient = line.split("public key:", 1)[1].strip()
    assert recipient.startswith("age1"), "could not parse the age recipient from the key file"

    plain = tmp_path / "device-creds.yaml"
    plain.write_text(_DECRYPTED_YAML, encoding="utf-8")
    enc = tmp_path / "device-creds.enc.yaml"
    with enc.open("wb") as fh:
        subprocess.run(
            [sops_bin, "--encrypt", "--age", recipient, str(plain)],
            stdout=fh,
            check=True,
        )

    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key_file))
    monkeypatch.setenv(creds.ENV_DEVICE_CREDS_FILE, str(enc))
    _kc_raises(monkeypatch, KeychainEntryMissingError("no entry"))

    assert creds.resolve_secret("admin", "bell-hub-admin") == "hub-sops-secret"
    assert creds.resolve_secret("bertrand@nepveu.name", "firewalla-app") == "fw-sops-token"


# ── wired into Firewalla's bridge-token resolution (env → file → resolver) ────


def test_firewalla_token_env_wins_over_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit FIREWALLA_BRIDGE_TOKEN env short-circuits the headless resolver."""
    from sanctum_cli.devices import firewalla as fw_mod

    monkeypatch.setenv("FIREWALLA_BRIDGE_TOKEN", "env-tok")

    def _boom(account: str, service: str) -> str:
        msg = "resolver must NOT run when the env token is present"
        raise AssertionError(msg)

    monkeypatch.setattr(creds, "resolve_secret_optional", _boom)
    assert fw_mod._read_bridge_token() == "env-tok"


def test_firewalla_token_falls_back_to_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """env + on-disk file both miss → the shared headless resolver supplies the token."""
    from sanctum_cli.devices import firewalla as fw_mod

    monkeypatch.delenv("FIREWALLA_BRIDGE_TOKEN", raising=False)
    monkeypatch.setattr(fw_mod, "_BRIDGE_TOKEN_FILE", tmp_path / "absent-token")
    seen: dict[str, str] = {}

    def _resolver(account: str, service: str) -> str:
        seen["account"], seen["service"] = account, service
        return "resolver-tok"

    monkeypatch.setattr(creds, "resolve_secret_optional", _resolver)
    assert fw_mod._read_bridge_token() == "resolver-tok"
    # It consults the firewalla-app service (the seeded headless tier).
    assert seen["service"] == "firewalla-app"


def test_firewalla_token_none_when_all_tiers_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All three tiers empty → None (fail-soft: detect/reads treat it as no bridge)."""
    from sanctum_cli.devices import firewalla as fw_mod

    monkeypatch.delenv("FIREWALLA_BRIDGE_TOKEN", raising=False)
    monkeypatch.setattr(fw_mod, "_BRIDGE_TOKEN_FILE", tmp_path / "absent-token")
    monkeypatch.setattr(creds, "resolve_secret_optional", lambda account, service: None)
    assert fw_mod._read_bridge_token() is None
