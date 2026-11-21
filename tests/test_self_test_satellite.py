"""``sanctum self-test`` satellite profile — ``instance.role: satellite``.

A satellite is a contained remote site: it carries model manifests for its own
local brain (which used to trip haus-tier detection) but runs none of the hub
services. On that profile hub probes are n/a, the local brain is probed, and
Vault Armor is probed wherever it is configured.

The local-brain probe is driven across real servers: a plain HTTP brain and a
mutual-TLS brain that demands a client certificate signed by a freshly minted
CA, so the router's TLS keys (``ca_cert_path`` / ``client_cert_path`` /
``client_key_path``) are proven end to end rather than asserted as strings.
"""

from __future__ import annotations

import contextlib
import datetime
import gc
import http.server
import ipaddress
import json
import socketserver
import ssl
import subprocess
import threading
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from rich.console import Console
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import self_test as st

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    target = tmp_path / "instance.yaml"
    target.write_text(body, encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))
    return target


SATELLITE_HEAD = "instance:\n  name: Second Haus\n  slug: second-haus\n  role: satellite\n"
SATELLITE = (
    SATELLITE_HEAD + "router:\n  default: local\n  backends:\n    local:\n"
    "      url: http://127.0.0.1:1338/v1\n"
)


def _satellite_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, name: str = "local"
) -> None:
    """A satellite whose ``router.default`` backend has the given (indented) keys."""
    _instance(
        tmp_path,
        monkeypatch,
        SATELLITE_HEAD + f"router:\n  default: '{name}'\n  backends:\n    '{name}':\n" + backend,
    )


# ── tiers ─────────────────────────────────────────────────────────────


def test_satellite_role_wins_over_haus_markers(tmp_path, monkeypatch) -> None:
    """Model manifests on disk must not grade a declared satellite as a hub."""
    monkeypatch.setattr(st.Path, "home", lambda: tmp_path)
    (tmp_path / ".sanctum/manifests").mkdir(parents=True)
    _instance(tmp_path, monkeypatch, SATELLITE)
    assert st._haus_tier_installed() is False


def test_haus_markers_still_mean_haus_without_role(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(st.Path, "home", lambda: tmp_path)
    (tmp_path / ".sanctum/manifests").mkdir(parents=True)
    _instance(tmp_path, monkeypatch, "instance:\n  name: Haus\n  slug: haus\n")
    assert st._haus_tier_installed() is True


def test_hub_role_is_haus_tier_without_any_marker(tmp_path, monkeypatch) -> None:
    """A declared hub is graded as one even before its services leave artifacts."""
    monkeypatch.setattr(st.Path, "home", lambda: tmp_path)  # no manifests, no proxyd dir
    _instance(tmp_path, monkeypatch, "instance:\n  name: Hub\n  slug: hub\n  role: hub\n")
    assert st._haus_tier_installed() is True
    ran = {"value": False}

    def hub_probe() -> st.ProbeResult:
        ran["value"] = True
        return st.ProbeResult(True, "ran")

    assert st._haus_only("proxyd", hub_probe)().detail == "ran"
    assert ran["value"] is True


def test_tier_label_follows_the_declared_role(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(st.Path, "home", lambda: tmp_path)
    (tmp_path / ".sanctum/manifests").mkdir(parents=True)
    _instance(tmp_path, monkeypatch, SATELLITE)
    assert st._tier_label() == "satellite"
    _instance(tmp_path, monkeypatch, "instance:\n  name: Hub\n  slug: hub\n  role: hub\n")
    assert st._tier_label() == "haus"
    monkeypatch.setattr(st, "_haus_tier_installed", lambda: False)
    _instance(tmp_path, monkeypatch, "instance:\n  name: Box\n  slug: box\n")
    assert st._tier_label() == "CLI"


def test_haus_only_probe_is_na_on_satellite(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, SATELLITE)
    ran = {"value": False}

    def hub_probe() -> st.ProbeResult:
        ran["value"] = True
        return st.ProbeResult(False, "must not run on a satellite")

    result = st._haus_only("proxyd", hub_probe)()
    assert result.not_applicable is True
    assert result.passed is True
    assert ran["value"] is False
    assert "satellite" in result.reason


def test_satellite_only_probe_is_na_elsewhere(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, "instance:\n  name: Haus\n  slug: haus\n")
    result = st._satellite_only("local-brain", lambda: st.ProbeResult(False, "no"))()
    assert result.not_applicable is True


# ── the summary names the satellite ───────────────────────────────────


def _two_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        st,
        "PROBES",
        [
            st.Probe("local brain", lambda: st.ProbeResult(True, "answers")),
            st.Probe(
                "hub service",
                lambda: st.ProbeResult(True, not_applicable=True, reason="runs on the hub"),
            ),
        ],
    )


def test_json_tier_is_satellite(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, SATELLITE)
    _two_probes(monkeypatch)
    result = CliRunner().invoke(app, ["self-test", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.find("{") : result.output.rfind("}") + 1])
    assert payload["tier"] == "satellite"
    assert payload["not_applicable"] == 1


def test_json_tier_is_reported_even_when_only_matches_no_probe(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, SATELLITE)
    result = CliRunner().invoke(app, ["self-test", "--json", "--only", "no-such-probe"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.find("{") : result.output.rfind("}") + 1])
    assert payload["tier"] == "satellite"
    assert (payload["total"], payload["not_applicable"], payload["probes"]) == (0, 0, [])


def test_human_headline_names_the_satellite(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, SATELLITE)
    _two_probes(monkeypatch)
    monkeypatch.setattr(st, "console", Console(width=200))  # keep the summary on one line
    result = CliRunner().invoke(app, ["self-test"])
    assert result.exit_code == 0, result.output
    assert "This Sanctum satellite is healthy." in result.output
    assert "1 n/a on this satellite" in result.output


# ── real servers for the local-brain probe ────────────────────────────


@dataclass
class _Brain:
    url: str  # the backend url as instance.yaml would hold it (…/v1)
    seen: list[dict[str, str]] = field(default_factory=list)
    status: int = 200
    redirect_to: str | None = None  # answer 302 with this Location instead


class _BrainHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        brain: _Brain = self.server.brain  # type: ignore[attr-defined]
        brain.seen.append(
            {"path": self.path, "authorization": self.headers.get("Authorization", "")}
        )
        if brain.redirect_to is not None:
            self.send_response(302)
            self.send_header("Location", brain.redirect_to)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        body = b'{"object":"list","data":[]}' if brain.status == 200 else b'{"error":"no"}'
        self.send_response(brain.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


class _BrainServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Handshakes eagerly so a rejected client is closed here, not at GC time."""

    daemon_threads = True
    block_on_close = False

    def get_request(self) -> tuple[object, object]:
        sock, addr = super().get_request()
        try:
            if isinstance(sock, ssl.SSLSocket):
                sock.do_handshake()
        except OSError:
            sock.close()
            raise  # socketserver drops it
        return sock, addr

    def handle_error(self, request: object, client_address: object) -> None:
        return  # expected: a client without (or with the wrong) certificate


@contextlib.contextmanager
def _serve(tls: ssl.SSLContext | None) -> Iterator[_Brain]:
    httpd = _BrainServer(("127.0.0.1", 0), _BrainHandler)
    scheme = "http"
    if tls is not None:
        httpd.socket = tls.wrap_socket(
            httpd.socket, server_side=True, do_handshake_on_connect=False
        )
        scheme = "https"
    brain = _Brain(url=f"{scheme}://127.0.0.1:{httpd.server_address[1]}/v1")
    httpd.brain = brain  # type: ignore[attr-defined]
    # A short poll keeps shutdown() (and so each teardown) fast.
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield brain
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)  # aborted handshakes only
            gc.collect()


@dataclass
class _PKI:
    ca: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    client_key_encrypted: Path


def _mint_pki(d: Path) -> _PKI:
    """CA + a 127.0.0.1 server leaf + a client leaf (plain and encrypted key)."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "satellite-test-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    def leaf(
        cn: str, usage: x509.ObjectIdentifier, ip: str | None = None
    ) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        )
        if ip:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]),
                critical=False,
            )
        return key, builder.sign(ca_key, hashes.SHA256())

    def cert_pem(cert: x509.Certificate, name: str) -> Path:
        p = d / name
        p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return p

    def key_pem(
        key: ec.EllipticCurvePrivateKey, name: str, passphrase: bytes | None = None
    ) -> Path:
        enc: serialization.KeySerializationEncryption = (
            serialization.BestAvailableEncryption(passphrase)
            if passphrase
            else serialization.NoEncryption()
        )
        p = d / name
        p.write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
        )
        p.chmod(0o600)
        return p

    d.mkdir(parents=True, exist_ok=True)
    server_key, server = leaf("127.0.0.1", ExtendedKeyUsageOID.SERVER_AUTH, ip="127.0.0.1")
    client_key, client = leaf("satellite-client", ExtendedKeyUsageOID.CLIENT_AUTH)
    return _PKI(
        ca=cert_pem(ca, "ca.crt"),
        server_cert=cert_pem(server, "server.crt"),
        server_key=key_pem(server_key, "server.key"),
        client_cert=cert_pem(client, "client.crt"),
        client_key=key_pem(client_key, "client.key"),
        client_key_encrypted=key_pem(client_key, "client-encrypted.key", b"correct horse"),
    )


@pytest.fixture
def pki(tmp_path: Path) -> _PKI:
    return _mint_pki(tmp_path / "pki")


@pytest.fixture
def mtls_brain(pki: _PKI) -> Iterator[_Brain]:
    """An https brain that refuses any client without a certificate from ``pki.ca``."""
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(pki.server_cert), str(pki.server_key))
    tls.verify_mode = ssl.CERT_REQUIRED
    tls.load_verify_locations(cafile=str(pki.ca))
    with _serve(tls) as brain:
        yield brain


@pytest.fixture
def plain_brain() -> Iterator[_Brain]:
    with _serve(None) as brain:
        yield brain


def _mtls_keys(pki: _PKI, *, key: Path | None = None) -> str:
    return (
        f"      ca_cert_path: {pki.ca}\n"
        f"      client_cert_path: {pki.client_cert}\n"
        f"      client_key_path: {key or pki.client_key}\n"
    )


class _RaisingOpener:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def open(self, request: object, timeout: float | None = None) -> object:
        raise self.exc


def _opener_raises(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Make the probe's own opener fail with ``exc`` (a transport error it must report)."""
    monkeypatch.setattr(st, "_brain_opener", lambda ctx: _RaisingOpener(exc))


class _ListenerServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    block_on_close = False


@contextlib.contextmanager
def _proxy_listener() -> Iterator[tuple[int, list[object]]]:
    """A port that records every connection and hangs up: a stand-in proxy."""
    accepted: list[object] = []

    class Hangup(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            accepted.append(self.client_address)

    server = _ListenerServer(("127.0.0.1", 0), Hangup)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1], accepted
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ── local brain: mutual TLS with the router's key names ───────────────


def test_canonical_tls_keys_pass_against_mtls_brain(tmp_path, monkeypatch, pki, mtls_brain) -> None:
    _satellite_backend(tmp_path, monkeypatch, f"      url: {mtls_brain.url}\n" + _mtls_keys(pki))
    result = st.probe_satellite_brain()
    assert result.passed is True, result.detail
    assert result.detail == f"local answers at {mtls_brain.url}"
    assert mtls_brain.seen[0]["path"] == "/v1/models"


def test_tls_paths_expand_the_home_directory(tmp_path, monkeypatch, pki, mtls_brain) -> None:
    monkeypatch.setenv("HOME", str(pki.ca.parent.parent))  # ~/pki/… resolves to the minted files
    keys = (
        "      ca_cert_path: ~/pki/ca.crt\n"
        "      client_cert_path: ~/pki/client.crt\n"
        "      client_key_path: ~/pki/client.key\n"
    )
    _satellite_backend(tmp_path, monkeypatch, f"      url: {mtls_brain.url}\n" + keys)
    assert st.probe_satellite_brain().passed is True


def test_uppercase_https_scheme_still_uses_tls(tmp_path, monkeypatch, pki, mtls_brain) -> None:
    url = mtls_brain.url.replace("https://", "HTTPS://")
    _satellite_backend(tmp_path, monkeypatch, f"      url: {url}\n" + _mtls_keys(pki))
    result = st.probe_satellite_brain()
    assert result.passed is True, result.detail


_RENAMED = (
    "local: ca, client_cert and client_key were renamed ca_cert_path, client_cert_path "
    "and client_key_path; rename them in instance.yaml"
)


def test_short_tls_key_names_fail_with_the_rename(tmp_path, monkeypatch, pki, mtls_brain) -> None:
    """ca / client_cert / client_key were pre-release names: say what they became, send nothing."""
    _satellite_backend(
        tmp_path,
        monkeypatch,
        f"      url: {mtls_brain.url}\n      ca: {pki.ca}\n"
        f"      client_cert: {pki.client_cert}\n      client_key: {pki.client_key}\n",
    )
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert result.detail == _RENAMED
    assert mtls_brain.seen == []


@pytest.mark.parametrize("short", ["ca", "client_cert", "client_key"])
@pytest.mark.parametrize("scheme", ["https", "http"])
def test_any_one_short_tls_key_fails_with_the_rename(tmp_path, monkeypatch, short, scheme) -> None:
    _satellite_backend(
        tmp_path, monkeypatch, f"      url: {scheme}://127.0.0.1:6502/v1\n      {short}: /x.pem\n"
    )
    monkeypatch.setattr(st, "_brain_opener", lambda _ctx: pytest.fail("no request may be sent"))
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert result.detail == _RENAMED


def test_a_short_key_beside_its_new_name_is_ignored(tmp_path, monkeypatch, pki, mtls_brain) -> None:
    """Both spellings during a migration: the router's own names are the ones used."""
    _satellite_backend(
        tmp_path,
        monkeypatch,
        f"      url: {mtls_brain.url}\n      ca: /stale/ca.crt\n"
        "      client_cert: /stale/client.crt\n      client_key: /stale/client.key\n"
        + _mtls_keys(pki),
    )
    result = st.probe_satellite_brain()
    assert result.passed is True, result.detail


def test_mtls_brain_refuses_a_client_without_a_cert(tmp_path, monkeypatch, pki, mtls_brain) -> None:
    """The fixture really demands a client certificate (so the passes above mean mTLS)."""
    _satellite_backend(
        tmp_path, monkeypatch, f"      url: {mtls_brain.url}\n      ca_cert_path: {pki.ca}\n"
    )
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert "not answering" in result.detail
    assert mtls_brain.seen == []


def test_certificate_required_alert_names_the_missing_keys(tmp_path, monkeypatch) -> None:
    """TLS 1.3 reports a missing client cert as an alert after the handshake; say what to set.

    Driven synthetically: against a live server the alert can lose a race with the
    TCP reset of a socket closed on unread request bytes.
    """
    _satellite_backend(tmp_path, monkeypatch, "      url: http://127.0.0.1:6502/v1\n")
    alert = ssl.SSLError(
        1, "[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required"
    )
    alert.reason = "TLSV13_ALERT_CERTIFICATE_REQUIRED"
    _opener_raises(monkeypatch, alert)
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert "tlsv13 alert certificate required" in result.detail
    assert result.detail.endswith(
        "; the server wants a client certificate: set client_cert_path and client_key_path)"
    )


def test_tls_keys_with_an_http_url_fail(tmp_path, monkeypatch, pki, plain_brain) -> None:
    _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}\n" + _mtls_keys(pki))
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert "TLS material is set" in result.detail
    assert "url is not https" in result.detail
    assert "ca_cert_path, client_cert_path, client_key_path" in result.detail
    assert plain_brain.seen == []  # never sent in the clear


def test_passphrase_protected_key_fails_fast(tmp_path, monkeypatch, pki, mtls_brain) -> None:
    """An encrypted client key must never reach OpenSSL's interactive prompt."""
    _satellite_backend(
        tmp_path,
        monkeypatch,
        f"      url: {mtls_brain.url}\n" + _mtls_keys(pki, key=pki.client_key_encrypted),
    )
    passwords: list[object] = []
    real_load = ssl.SSLContext.load_cert_chain

    def spy(
        self: ssl.SSLContext, certfile: str, keyfile: object = None, password: object = None
    ) -> None:
        passwords.append(password)
        real_load(self, certfile, keyfile, password)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", spy)
    box: dict[str, st.ProbeResult] = {}
    worker = threading.Thread(target=lambda: box.update(r=st.probe_satellite_brain()), daemon=True)
    worker.start()
    worker.join(timeout=15)
    assert not worker.is_alive(), "probe blocked on a passphrase prompt"
    result = box["r"]
    assert result.passed is False
    assert result.detail == (
        "local: client_key_path is passphrase-protected; use an unencrypted key (mode 0600)"
    )
    assert len(passwords) == 1
    callback = passwords[0]
    assert callable(callback)
    assert callback() == b""
    assert mtls_brain.seen == []


def test_passphrase_protected_key_inside_the_cert_file_names_both_places(
    tmp_path, monkeypatch, pki, mtls_brain
) -> None:
    """With client_key_path unset, the encrypted key is the one bundled in client_cert_path."""
    bundle = tmp_path / "client-bundle.pem"
    bundle.write_bytes(pki.client_cert.read_bytes() + pki.client_key_encrypted.read_bytes())
    bundle.chmod(0o600)
    _satellite_backend(
        tmp_path,
        monkeypatch,
        f"      url: {mtls_brain.url}\n      ca_cert_path: {pki.ca}\n"
        f"      client_cert_path: {bundle}\n",
    )
    box: dict[str, st.ProbeResult] = {}
    worker = threading.Thread(target=lambda: box.update(r=st.probe_satellite_brain()), daemon=True)
    worker.start()
    worker.join(timeout=15)
    assert not worker.is_alive(), "probe blocked on a passphrase prompt"
    assert box["r"].passed is False
    assert box["r"].detail == (
        "local: the client key (client_key_path, or the key inside client_cert_path) is "
        "passphrase-protected; use an unencrypted key (mode 0600)"
    )
    assert mtls_brain.seen == []


def test_missing_ca_file_is_named(tmp_path, monkeypatch, pki, mtls_brain) -> None:
    missing = tmp_path / "nope" / "ca.crt"
    _satellite_backend(
        tmp_path, monkeypatch, f"      url: {mtls_brain.url}\n      ca_cert_path: {missing}\n"
    )
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert result.detail == f"local: ca_cert_path not found: {missing}"


def test_client_key_without_client_cert_fails(tmp_path, monkeypatch, pki, mtls_brain) -> None:
    _satellite_backend(
        tmp_path,
        monkeypatch,
        f"      url: {mtls_brain.url}\n      ca_cert_path: {pki.ca}\n"
        f"      client_key_path: {pki.client_key}\n",
    )
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert "client_key_path is set without client_cert_path" in result.detail


# ── local brain: auth, names, status codes ────────────────────────────


def test_brain_answering_200_passes(tmp_path, monkeypatch, plain_brain) -> None:
    _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}/\n")
    result = st.probe_satellite_brain()
    assert result.passed is True
    assert plain_brain.seen == [{"path": "/v1/models", "authorization": ""}]


def test_api_key_env_is_sent_as_bearer(tmp_path, monkeypatch, plain_brain) -> None:
    monkeypatch.setenv("SATELLITE_TEST_BRAIN_KEY", "tok-123")
    _satellite_backend(
        tmp_path,
        monkeypatch,
        f"      url: {plain_brain.url}\n      api_key_env: SATELLITE_TEST_BRAIN_KEY\n",
    )
    result = st.probe_satellite_brain()
    assert result.passed is True
    assert plain_brain.seen[0]["authorization"] == "Bearer tok-123"
    assert "tok-123" not in result.detail


@pytest.mark.parametrize("status", [401, 403])
def test_auth_challenge_counts_as_answering(tmp_path, monkeypatch, plain_brain, status) -> None:
    plain_brain.status = status
    _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}\n")
    result = st.probe_satellite_brain()
    assert result.passed is True
    assert result.detail == f"local answers at {plain_brain.url} (HTTP {status}: needs auth)"


def test_auth_challenge_notes_an_unset_api_key_env(tmp_path, monkeypatch, plain_brain) -> None:
    plain_brain.status = 401
    monkeypatch.delenv("SATELLITE_TEST_BRAIN_KEY", raising=False)
    _satellite_backend(
        tmp_path,
        monkeypatch,
        f"      url: {plain_brain.url}\n      api_key_env: SATELLITE_TEST_BRAIN_KEY\n",
    )
    result = st.probe_satellite_brain()
    assert result.passed is True
    assert "(HTTP 401: needs auth; $SATELLITE_TEST_BRAIN_KEY is not set here)" in result.detail
    assert plain_brain.seen[0]["authorization"] == ""


def test_auth_challenge_notes_a_rejected_key(tmp_path, monkeypatch, plain_brain) -> None:
    plain_brain.status = 401
    monkeypatch.setenv("SATELLITE_TEST_BRAIN_KEY", "tok-wrong")
    _satellite_backend(
        tmp_path,
        monkeypatch,
        f"      url: {plain_brain.url}\n      api_key_env: SATELLITE_TEST_BRAIN_KEY\n",
    )
    result = st.probe_satellite_brain()
    assert result.passed is True
    assert "$SATELLITE_TEST_BRAIN_KEY was rejected" in result.detail
    assert "tok-wrong" not in result.detail


def test_not_found_fails_with_a_base_path_hint(tmp_path, monkeypatch, plain_brain) -> None:
    plain_brain.status = 404
    _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}\n")
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert "(HTTP 404; does the url end with the API base path, e.g. /v1?)" in result.detail


def test_dotted_backend_name_resolves(tmp_path, monkeypatch, plain_brain) -> None:
    """``router.default: brain.v2`` must not be split into router.backends.brain.v2."""
    _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}\n", name="brain.v2")
    result = st.probe_satellite_brain()
    assert result.passed is True, result.detail
    assert result.detail.startswith("brain.v2 answers at ")


def test_satellite_brain_fails_when_backend_down(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, SATELLITE)
    _opener_raises(monkeypatch, st.urllib.error.URLError("connection refused"))
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert "not answering" in result.detail
    assert "connection refused" in result.detail


# ── local brain: the token never leaks ────────────────────────────────

_KEY_ENV = "      api_key_env: SATELLITE_TEST_BRAIN_KEY\n"


def test_token_with_a_trailing_newline_is_sent_bare_and_never_printed(
    tmp_path, monkeypatch, plain_brain
) -> None:
    """``export KEY="$(cat file)"`` habits leave a newline; http.client would quote the
    whole header value, token included, in the ValueError it raises."""
    monkeypatch.setenv("SATELLITE_TEST_BRAIN_KEY", "tok-secret-123\n")
    _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}\n" + _KEY_ENV)

    result = st.probe_satellite_brain()
    assert result.passed is True, result.detail
    assert "tok-secret-123" not in result.detail
    assert plain_brain.seen[0]["authorization"] == "Bearer tok-secret-123"

    out = CliRunner().invoke(app, ["self-test", "--json", "--only", "satellite local brain"])
    assert "tok-secret-123" not in out.output
    payload = json.loads(out.output[out.output.find("{") : out.output.rfind("}") + 1])
    assert payload["probes"][0]["passed"] is True


@pytest.mark.parametrize("token", ["tok\nsecret", "tok\r\nX-Injected: secret", "tok\x1bsecret"])
def test_token_with_a_control_character_fails_without_naming_it(
    tmp_path, monkeypatch, plain_brain, token
) -> None:
    monkeypatch.setenv("SATELLITE_TEST_BRAIN_KEY", token)
    _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}\n" + _KEY_ENV)

    result = st.probe_satellite_brain()

    assert result.passed is False
    assert result.detail == (
        "local: $SATELLITE_TEST_BRAIN_KEY holds a control character (a stray line break?); "
        "set it to the bare token"
    )
    assert plain_brain.seen == []  # nothing was sent


def test_a_header_http_rejects_fails_with_a_fixed_message(
    tmp_path, monkeypatch, plain_brain
) -> None:
    """A ValueError from urllib / http.client may quote the token: its text is never shown."""
    monkeypatch.setenv("SATELLITE_TEST_BRAIN_KEY", "tok-€-secret")  # not latin-1: http rejects it
    _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}\n" + _KEY_ENV)

    result = st.probe_satellite_brain()

    assert result.passed is False
    assert result.detail == (
        f"local: could not send a request to {plain_brain.url} "
        "(invalid header or url; check $SATELLITE_TEST_BRAIN_KEY and the url)"
    )


def test_a_redirect_fails_and_the_token_never_reaches_the_other_origin(
    tmp_path, monkeypatch, plain_brain
) -> None:
    monkeypatch.setenv("SATELLITE_TEST_BRAIN_KEY", "tok-redirect-secret")
    with _serve(None) as elsewhere:
        plain_brain.redirect_to = f"{elsewhere.url}/models"
        _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}\n" + _KEY_ENV)

        result = st.probe_satellite_brain()

        assert result.passed is False
        assert result.detail == (
            f"local not answering at {plain_brain.url} (HTTP 302: redirects to "
            f"{elsewhere.url}/models; set url to the API base itself)"
        )
        assert plain_brain.seen[0]["authorization"] == "Bearer tok-redirect-secret"
        assert elsewhere.seen == []


def test_a_redirect_location_is_printed_without_control_characters(
    tmp_path, monkeypatch, plain_brain
) -> None:
    plain_brain.redirect_to = "http://elsewhere.test/\x1b[31mred"
    _satellite_backend(tmp_path, monkeypatch, f"      url: {plain_brain.url}\n")

    result = st.probe_satellite_brain()

    assert result.passed is False
    assert "redirects to http://elsewhere.test/?[31mred;" in result.detail
    assert "\x1b" not in result.detail


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_proxy_environment_is_ignored(tmp_path, monkeypatch, request, scheme) -> None:
    """The brain is local: a proxy from the environment would only read the request."""
    # urlopen() keeps the first opener it builds, with the proxies it read then; drop
    # it so this test sees what a fresh `sanctum self-test` process would.
    monkeypatch.setattr(st.urllib.request, "_opener", None)
    with _proxy_listener() as (port, accepted):
        for var in ("http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(var, f"http://127.0.0.1:{port}")
            monkeypatch.setenv(var.upper(), f"http://127.0.0.1:{port}")
        for var in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(var, raising=False)
        if scheme == "https":
            pki = request.getfixturevalue("pki")
            brain = request.getfixturevalue("mtls_brain")
            keys = _mtls_keys(pki)
        else:
            brain = request.getfixturevalue("plain_brain")
            keys = ""
        monkeypatch.setenv("SATELLITE_TEST_BRAIN_KEY", "tok-proxy-secret")
        _satellite_backend(tmp_path, monkeypatch, f"      url: {brain.url}\n" + keys + _KEY_ENV)

        result = st.probe_satellite_brain()

        assert result.passed is True, result.detail
        assert accepted == []
        assert brain.seen[0]["authorization"] == "Bearer tok-proxy-secret"


# ── local brain: a missing or malformed router block shows the shape ──

_SHAPE_TAIL = (
    "router.backends.{key}.url: https://127.0.0.1:6502/v1 "
    "(plus ca_cert_path / client_cert_path / client_key_path for mTLS)"
)


def test_missing_router_block_shows_the_expected_shape(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, SATELLITE_HEAD)
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert result.detail == (
        "router.default is not set; set router.default: <key> and "
        + _SHAPE_TAIL.format(key="<key>")
    )
    assert "\n" not in result.detail


@pytest.mark.parametrize(("value", "shown"), [("3", "3"), ("[local]", "['local']")])
def test_a_router_default_that_is_not_a_string_says_so(tmp_path, monkeypatch, value, shown) -> None:
    _instance(tmp_path, monkeypatch, SATELLITE_HEAD + f"router:\n  default: {value}\n")
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert result.detail == (
        f"router.default must name a backend (a string), got {shown}; set router.default: "
        "<key> and " + _SHAPE_TAIL.format(key="<key>")
    )


def test_missing_backend_shows_the_shape_for_that_key(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, SATELLITE_HEAD + "router:\n  default: local\n")
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert result.detail.startswith("router.backends.local is missing; set router.default: local")
    assert result.detail.endswith(_SHAPE_TAIL.format(key="local"))


def test_missing_backend_url_shows_the_shape_for_that_key(tmp_path, monkeypatch) -> None:
    _satellite_backend(tmp_path, monkeypatch, "      description: no url yet\n")
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert result.detail.startswith("router.backends.local.url is not set; ")
    assert "\n" not in result.detail


def test_url_without_a_scheme_fails_with_the_shape(tmp_path, monkeypatch) -> None:
    _satellite_backend(tmp_path, monkeypatch, "      url: 127.0.0.1:6502/v1\n")
    result = st.probe_satellite_brain()
    assert result.passed is False
    assert "is not an http:// or https:// url" in result.detail
    assert result.detail.endswith(_SHAPE_TAIL.format(key="local"))


# ── vault armor ───────────────────────────────────────────────────────


def _vault_manifest(home: Path) -> None:
    d = home / ".sanctum/vault-armor"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps({"volume_name": "SanctumVault", "envelope": {"volume_uuid": "UUID-1"}}),
        encoding="utf-8",
    )


def _fake_diskutil(monkeypatch, stdout: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(st.subprocess, "run", fake_run)
    return calls


def test_vault_armor_na_when_not_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(st.Path, "home", lambda: tmp_path)
    result = st.probe_vault_armor()
    assert result.not_applicable is True


def test_vault_armor_calls_diskutil_by_absolute_path(tmp_path, monkeypatch) -> None:
    """A PATH-planted ``diskutil`` must not get to answer for the system one."""
    monkeypatch.setattr(st.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "planted-bin"))
    _vault_manifest(tmp_path)
    calls = _fake_diskutil(monkeypatch, "   Locked:    No\n")
    st.probe_vault_armor()
    assert calls == [["/usr/sbin/diskutil", "info", "UUID-1"]]


def test_vault_armor_passes_when_unlocked(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(st.Path, "home", lambda: tmp_path)
    _vault_manifest(tmp_path)
    _fake_diskutil(monkeypatch, "   Mounted:   Yes\n   Locked:    No\n")
    result = st.probe_vault_armor()
    assert result.passed is True
    assert result.not_applicable is False


def test_vault_armor_fails_when_locked(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(st.Path, "home", lambda: tmp_path)
    _vault_manifest(tmp_path)
    _fake_diskutil(monkeypatch, "   Mounted:   No\n   Locked:    Yes\n")
    result = st.probe_vault_armor()
    assert result.passed is False
    assert "locked" in result.detail


def test_vault_armor_fails_when_volume_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(st.Path, "home", lambda: tmp_path)
    _vault_manifest(tmp_path)
    _fake_diskutil(monkeypatch, "Could not find disk: UUID-1\n")
    result = st.probe_vault_armor()
    assert result.passed is False
    assert "not found" in result.detail
