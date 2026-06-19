"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="session", autouse=True)
def _tls_ca_for_tests(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Make TLS code paths hermetic on a CA-less machine (CI / a clean checkout).

    proxyd/council build an SSL context from ``$SANCTUM_PROXYD_CA`` else
    ``~/.sanctum/certs/ca.crt``; with neither present, ``create_default_context``
    raises ``FileNotFoundError``. When the real haus CA is absent, mint a
    throwaway CA and point the CA env vars at it so those paths load a valid
    cafile. No-op when a real CA exists (tests use it); never writes to
    ``~/.sanctum``.
    """
    if Path("~/.sanctum/certs/ca.crt").expanduser().exists():
        yield
        return
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sanctum-test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca = tmp_path_factory.mktemp("sanctum-test-ca") / "ca.crt"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    for env in ("SANCTUM_PROXYD_CA", "SANCTUM_COUNCIL_CACERT", "SANCTUM_COUNCIL_CA"):
        os.environ.setdefault(env, str(ca))
    yield


@pytest.fixture(autouse=True)
def _haus_present(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the haus-only gate deterministic across hosts.

    The real :func:`sanctum_cli.haus.is_present` is host-dependent (it checks the
    mTLS CA, devices.yaml, LaunchAgents) — so a haus-only command test would pass
    on Bert's box and banner-out on CI. Default every test to "haus present" so
    gated commands run their real bodies everywhere. Tests that exercise the gate
    itself opt out with ``@pytest.mark.no_haus_stub`` (e.g. tests/test_haus.py)
    and inject presence/absence themselves.
    """
    if "no_haus_stub" in request.keywords:
        return
    monkeypatch.setattr("sanctum_cli.haus.is_present", lambda _component: True)


@pytest.fixture
def minimal_instance_yaml(tmp_path: Path) -> Path:
    """A tiny but valid instance.yaml — instance block only, defaults everywhere else."""
    p = tmp_path / "instance.yaml"
    p.write_text(
        "instance:\n"
        "  name: Test Instance\n"
        "  slug: test-instance\n"
        "  timezone: UTC\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def full_instance_yaml(tmp_path: Path) -> Path:
    """instance.yaml exercising every cli: subkey."""
    p = tmp_path / "instance.yaml"
    p.write_text(
        """\
instance:
  name: Manoir
  slug: manoir
  timezone: America/New_York

notifications:
  owner_name: Test Operator
  signal:
    enabled: true
    target: '+15555550100'

cli:
  default_provider: claude
  routing:
    rules:
      - when: { has_image: true }
        then: gemini
      - when: { intent: code }
        then: claude
    fallback: claude
  providers:
    claude:
      via: direct
      endpoint: https://api.anthropic.com
      model: claude-opus-4-7
    gemini:
      model: gemini-2.5-pro
    mlx_local:
      endpoint: http://127.0.0.1:8900
      always_available: true
  telemetry:
    enabled: true
    redact_prompts: true
    aggregate_window_days: 7
  cloud_backup:
    primary:
      kind: restic
      repo: /Volumes/T9/sanctum-restic
      keychain:
        service: sanctum-backup-key
        account: sanctum-backup
    secondary:
      kind: restic
      repo: rclone:gdrive-sanctum:sanctum-restic
      keychain:
        service: sanctum-backup-key
        account: sanctum-backup
""",
        encoding="utf-8",
    )
    return p
