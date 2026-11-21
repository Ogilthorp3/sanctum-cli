"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_INSTANCE_ENV = "SANCTUM_INSTANCE_FILE"
# The CA bundles proxyd, council and brainstorm read before ~/.sanctum/certs/ca.crt.
_CA_ENVS = ("SANCTUM_PROXYD_CA", "SANCTUM_COUNCIL_CACERT", "SANCTUM_COUNCIL_CA")
_saved_env: list[dict[str, str | None]] = []
_test_ca_dirs: list[Path] = []


def pytest_configure(config: pytest.Config) -> None:
    """Even collection must not read the developer's real ``instance.yaml`` or certificates.

    Test modules import ``sanctum_cli.commands.council`` while pytest collects them, and
    its ``SEATS`` table reads ``instance.yaml`` at import time (the operator and host
    labels in Yoda's persona), before any fixture runs. Point the instance file at a path
    that does not exist for the whole session; the autouse fixture below repoints it at
    each test's fake home.

    The TLS code reads ``$SANCTUM_PROXYD_CA`` / ``$SANCTUM_COUNCIL_CACERT`` before
    ``~/.sanctum/certs/ca.crt``, and ``brainstorm`` binds its default ``--cacert`` at import.
    On every machine, with or without a real CA (and whatever the shell exports), point
    all three at a throwaway CA minted here, so no test loads the developer's CA and a
    CA-less CI box still has a valid cafile.
    """
    _saved_env.append({name: os.environ.get(name) for name in (_INSTANCE_ENV, *_CA_ENVS)})
    missing = Path(tempfile.gettempdir()) / f"sanctum-tests-{os.getpid()}-absent"
    os.environ[_INSTANCE_ENV] = str(missing / "instance.yaml")
    ca_dir = Path(tempfile.mkdtemp(prefix="sanctum-test-ca-"))
    _test_ca_dirs.append(ca_dir)
    ca = _mint_test_ca(ca_dir / "ca.crt")
    for name in _CA_ENVS:
        os.environ[name] = str(ca)


def pytest_unconfigure(config: pytest.Config) -> None:
    if _saved_env:
        for name, saved in _saved_env.pop().items():
            if saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved
    while _test_ca_dirs:
        shutil.rmtree(_test_ca_dirs.pop(), ignore_errors=True)


def _mint_test_ca(dest: Path) -> Path:
    """Write a self-signed CA certificate (PEM, valid for two days) to ``dest``; no key kept."""
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
    dest.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return dest


@pytest.fixture(autouse=True)
def _home_is_never_the_operators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test writes into the operator's real ``~/.sanctum``.

    On 2026-09-19 a full-suite run appended 215 lines to the real
    ``~/.sanctum/telemetry/cli.jsonl`` and 10 to ``~/.sanctum/logs/netgear-audit.jsonl``, and
    overwrote ``~/.sanctum/wifi-mac-stability.mobileconfig``. ``HOME`` alone is not enough:
    several defaults are bound to ``Path.home()`` at IMPORT time, so they are patched by name
    here. A test that wants the real home asks for it explicitly.

    Two more escape HOME. The default instance file is resolved at import time, so without
    ``SANCTUM_INSTANCE_FILE`` every test reads the developer's real ``instance.yaml`` (and a
    satellite's ``instance.role`` silently regrades every tier test). And
    ``service_user.operator_home()`` finds the login user through ``pwd``, so anything that
    materializes the wave-1 plists (``onboard --recipe operator --yes``) would write them into
    the real ``~/.sanctum/launchdaemons``. Both point at the fake home instead.
    """
    fake = tmp_path / "home"
    (fake / ".sanctum").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake))
    monkeypatch.setenv(_INSTANCE_ENV, str(fake / ".sanctum" / "instance.yaml"))
    monkeypatch.setattr("sanctum_cli.service_user.operator_home", lambda: fake)
    for mod_name, attr, rel in (
        ("sanctum_cli.devices.rails", "DEFAULT_AUDIT_LOG", ".sanctum/logs/netgear-audit.jsonl"),
        (
            "sanctum_cli.service_user",
            "INSTALL_SCRIPT",
            ".sanctum/scripts/service-user/install-on-new-hub.sh",
        ),
        ("sanctum_cli.service_user", "STATUS_SCRIPT", ".sanctum/scripts/service-user/status.sh"),
    ):
        try:
            mod = __import__(mod_name, fromlist=["_"])
        except Exception:  # a module this suite never loads
            continue
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, fake / rel, raising=False)


@pytest.fixture(autouse=True)
def _haus_present(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the haus-only gate deterministic across hosts.

    The real :func:`sanctum_cli.haus.is_present` is host-dependent (it checks the
    mTLS CA, devices.yaml, LaunchAgents) — so a haus-only command test would pass
    on an operator's box and banner-out on CI. Default every test to "haus present" so
    gated commands run their real bodies everywhere. Tests that exercise the gate
    itself opt out with ``@pytest.mark.no_haus_stub`` (e.g. tests/test_haus.py)
    and inject presence/absence themselves.
    """
    if "no_haus_stub" in request.keywords:
        return
    monkeypatch.setattr("sanctum_cli.haus.is_present", lambda _component: True)


@pytest.fixture(autouse=True)
def _isolate_mesh_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the mesh tracker default off any LIVE loopback service.

    The default tracker URL is ``http://127.0.0.1:8765``. On ephemeral CI that
    port is closed, so an incidental tracker probe during a command refuses
    instantly and leaves no socket. On a dev box (or a self-hosted runner) a real
    tracker may be listening on 8765 — the probe then *connects*, and the unclosed
    client socket raises a ``ResourceWarning`` that ``filterwarnings=error`` turns
    into a failure in some unrelated later test. Point the default at a
    just-closed ephemeral port so tests never reach a live tracker and behave
    identically on every host (and are safe on a self-hosted runner). Tests that
    exercise tracker wiring set ``mesh.tracker_url`` themselves and are unaffected.
    """
    import socket as _socket

    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()  # port is now free/closed → connections refuse fast, no leaked socket
    monkeypatch.setattr(
        "sanctum_cli.commands.mesh._DEFAULT_TRACKER_URL",
        f"http://127.0.0.1:{closed_port}",
    )


@pytest.fixture
def minimal_instance_yaml(tmp_path: Path) -> Path:
    """A tiny but valid instance.yaml — instance block only, defaults everywhere else."""
    p = tmp_path / "instance.yaml"
    p.write_text(
        "instance:\n  name: Test Instance\n  slug: test-instance\n  timezone: UTC\n",
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
  name: Home
  slug: home
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
    path: 'TELEMETRY_PATH'
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
""".replace("TELEMETRY_PATH", str(tmp_path / "telemetry.jsonl")),
        encoding="utf-8",
    )
    return p
