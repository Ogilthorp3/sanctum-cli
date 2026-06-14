"""Transport-boundary tests for ``sanctum_cli.proxyd``.

Per the Contracts-at-the-Boundary doctrine, these do NOT assert that a config
field equals a string. They stand up a **real TLS server** with a freshly
minted EC P-256 CA + leaf and drive the actual client paths across it:

* httpx (``council``) with ``verify=<ca>`` completes a CA-verified handshake.
* urllib + ssl (``self-test`` probe) completes the same handshake.
* the wrong CA is REJECTED (the verify boundary actually holds).
* an ``https://`` URL against a PLAINTEXT listener fails fast (the cutover
  footgun) and the error hint is debuggable.

The certs are EC P-256 — the same classical curve sanctum ships today — but the
client code pins no curve or KEM, so this stays valid when the server later
offers a hybrid ML-KEM group. Only ``cryptography`` (already a transitive dep)
and the stdlib are used; no live proxyd, no network.
"""

from __future__ import annotations

import datetime
import gc
import http.server
import ipaddress
import socketserver
import ssl
import threading
import urllib.error
import urllib.request
import warnings
from typing import TYPE_CHECKING

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from sanctum_cli import proxyd
from sanctum_cli.commands import self_test as st

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ── Cert minting (EC P-256, mirrors sanctum's real ca.crt/server.crt) ──


def _mint_ca_and_leaf(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Mint a CA + a 127.0.0.1 leaf signed by it. Returns (ca, leaf, key)."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sanctum-test-ca")])
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
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = tmp_path / "ca.crt"
    leaf_pem = tmp_path / "server.crt"
    key_pem = tmp_path / "server.key"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    leaf_pem.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, leaf_pem, key_pem


def _mint_unrelated_ca(tmp_path: Path) -> Path:
    """A second, unrelated CA — used to prove the verify boundary rejects it."""
    now = datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other-ca")])
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
    other = tmp_path / "other-ca.crt"
    other.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return other


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    """Answers /v1/models with 401 — the real proxyd reply to a keyless GET."""

    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"missing x-api-key"}')

    def log_message(self, *_args: object) -> None:  # silence the test log
        return


class _QuietTLSServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """An HTTPServer whose handshake/connection errors are swallowed.

    The wrong-CA and plaintext-footgun cases ABORT the TLS handshake. Left
    alone, the server thread's aborted ``SSLSocket`` gets GC'd and raises an
    unraisable-exception warning at teardown — and ``filterwarnings=["error"]``
    turns that into a spurious failure. We *expect* those aborts, so we do the
    handshake eagerly in :meth:`get_request` and, on failure, close the socket
    cleanly and raise ``OSError`` (which ``socketserver`` drops via
    :meth:`handle_error`). Nothing dangling, nothing unraisable.
    """

    daemon_threads = True
    block_on_close = False

    def get_request(self) -> tuple[object, object]:
        sock, addr = super().get_request()
        try:
            # The socket is already TLS-wrapped (deferred handshake); force it
            # now so a reject is caught here rather than at GC time.
            if isinstance(sock, ssl.SSLSocket):
                sock.do_handshake()
        except OSError:
            try:
                sock.close()
            finally:
                raise  # socketserver routes this to handle_error → dropped
        return sock, addr

    def handle_error(self, request: object, client_address: object) -> None:
        # Expected: the client rejected our cert / spoke plaintext. Not a bug.
        return


def _shutdown(httpd: http.server.HTTPServer, thread: threading.Thread) -> None:
    """Tear a test server down and finalize any aborted-handshake socket here.

    The wrong-CA / plaintext-footgun cases leave a closed server-side SSLSocket
    whose ``__del__`` emits a benign ResourceWarning. We force its collection
    INSIDE this teardown and absorb only that one warning class — the global
    ``filterwarnings=["error"]`` policy is untouched for every real warning.
    """
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()


@pytest.fixture
def tls_proxyd(tmp_path: Path) -> Iterator[tuple[str, Path, Path]]:
    """A real TLS server impersonating proxyd. Yields (base_url, ca, wrong_ca)."""
    ca_pem, leaf_pem, key_pem = _mint_ca_and_leaf(tmp_path)
    wrong_ca = _mint_unrelated_ca(tmp_path)
    httpd = _QuietTLSServer(("127.0.0.1", 0), _QuietHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(leaf_pem), keyfile=str(key_pem))
    # Defer the handshake to get_request() so an aborted handshake is caught and
    # closed there (no dangling SSLSocket → no unraisable warning at teardown).
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{port}", ca_pem, wrong_ca
    finally:
        _shutdown(httpd, thread)


@pytest.fixture
def plaintext_proxyd() -> Iterator[str]:
    """A real PLAINTEXT server — the cutover footgun (https URL → plain port)."""
    httpd = _QuietTLSServer(("127.0.0.1", 0), _QuietHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        _shutdown(httpd, thread)


# ── Resolution policy (pure) ──────────────────────────────────────────


class TestResolution:
    def test_default_is_tls_and_ca_secure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Point the CA env at a real PEM so create_default_context can load it.
        ca_pem, _leaf, _key = _mint_ca_and_leaf(tmp_path)
        for var in (proxyd.URL_ENV, proxyd.INSECURE_ENV):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv(proxyd.CA_ENV, str(ca_pem))
        assert proxyd.base_url() == "https://127.0.0.1:4040"
        assert proxyd.is_https() is True
        # verify() defaults to a verifying SSLContext, never False.
        v = proxyd.verify()
        assert v is not False
        assert isinstance(v, ssl.SSLContext)
        assert v.verify_mode == ssl.CERT_REQUIRED  # CA-pinned, secure default

    def test_default_url_constant_is_tls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Independent of any CA file: the standing endpoint is https on :4040.
        monkeypatch.delenv(proxyd.URL_ENV, raising=False)
        assert proxyd.DEFAULT_URL == "https://127.0.0.1:4040"
        assert proxyd.base_url() == "https://127.0.0.1:4040"

    def test_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(proxyd.URL_ENV, "https://127.0.0.1:4041/")
        assert proxyd.base_url() == "https://127.0.0.1:4041"  # trailing / stripped

    def test_ca_override_loads_the_named_bundle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ca_pem, _leaf, _key = _mint_ca_and_leaf(tmp_path)
        monkeypatch.delenv(proxyd.INSECURE_ENV, raising=False)
        monkeypatch.setenv(proxyd.CA_ENV, str(ca_pem))
        ctx = proxyd.ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_insecure_must_be_explicit_and_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(proxyd.INSECURE_ENV, "1")
        assert proxyd.verify() is False
        # The dev hatch yields an unverified context (CERT_NONE) for the ssl path.
        assert proxyd.ssl_context().verify_mode == ssl.CERT_NONE

    def test_insecure_falsey_value_stays_secure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Only the truthy set disables verification; a stray "0"/"" never does.
        ca_pem, _leaf, _key = _mint_ca_and_leaf(tmp_path)
        monkeypatch.setenv(proxyd.CA_ENV, str(ca_pem))
        monkeypatch.setenv(proxyd.INSECURE_ENV, "0")
        assert proxyd.verify() is not False
        assert proxyd.ssl_context().verify_mode == ssl.CERT_REQUIRED


# ── Real TLS handshake — the boundary, not a string ───────────────────


class TestTlsBoundary:
    def test_httpx_ca_verify_handshake_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, tls_proxyd: tuple[str, Path, Path]
    ) -> None:
        """The council client path: httpx with verify=proxyd.verify() (an
        SSLContext) completes a real TLS handshake and reaches the route
        (401 = handshake passed, keyless)."""
        base, ca_pem, _wrong = tls_proxyd
        monkeypatch.setenv(proxyd.CA_ENV, str(ca_pem))
        monkeypatch.delenv(proxyd.INSECURE_ENV, raising=False)
        # Exactly what council.py constructs: httpx.Client(verify=proxyd.verify()).
        with httpx.Client(verify=proxyd.verify()) as client:
            resp = client.get(f"{base}/v1/models")
        assert resp.status_code == 401  # TLS succeeded; server rejected the keyless GET

    def test_httpx_wrong_ca_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tls_proxyd: tuple[str, Path, Path]
    ) -> None:
        """The verify boundary actually holds: an unrelated CA fails the
        handshake. If this passed, verification would be theatre."""
        base, _ca, wrong_ca = tls_proxyd
        monkeypatch.setenv(proxyd.CA_ENV, str(wrong_ca))
        monkeypatch.delenv(proxyd.INSECURE_ENV, raising=False)
        with pytest.raises(httpx.ConnectError), httpx.Client(verify=proxyd.verify()) as client:
            client.get(f"{base}/v1/models")

    def test_urllib_ssl_ca_verify_handshake_succeeds(
        self, tls_proxyd: tuple[str, Path, Path]
    ) -> None:
        """The self-test probe path: ssl.create_default_context(cafile=<ca>)
        passed to urlopen completes the same CA-verified handshake."""
        base, ca_pem, _wrong = tls_proxyd
        ctx = ssl.create_default_context(cafile=str(ca_pem))
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(
                urllib.request.Request(f"{base}/v1/models"), timeout=3, context=ctx
            )
        assert exc_info.value.code == 401  # handshake passed, server returned 401

    def test_probe_proxyd_passes_against_real_tls(
        self, monkeypatch: pytest.MonkeyPatch, tls_proxyd: tuple[str, Path, Path]
    ) -> None:
        """End-to-end: the actual probe_proxyd() function, pointed at a real
        CA-verified TLS endpoint via the env overrides, reports alive on 401."""
        base, ca_pem, _wrong = tls_proxyd
        monkeypatch.setenv(proxyd.URL_ENV, base)
        monkeypatch.setenv(proxyd.CA_ENV, str(ca_pem))
        monkeypatch.delenv(proxyd.INSECURE_ENV, raising=False)
        res = st.probe_proxyd()
        assert res.passed is True
        assert "401" in res.detail  # service alive, TLS verified

    def test_probe_proxyd_fails_with_wrong_ca(
        self, monkeypatch: pytest.MonkeyPatch, tls_proxyd: tuple[str, Path, Path]
    ) -> None:
        """A CA that did not sign the leaf must make the probe FAIL — proving the
        probe verifies the chain rather than blindly accepting any TLS server."""
        base, _ca, wrong_ca = tls_proxyd
        monkeypatch.setenv(proxyd.URL_ENV, base)
        monkeypatch.setenv(proxyd.CA_ENV, str(wrong_ca))
        monkeypatch.delenv(proxyd.INSECURE_ENV, raising=False)
        res = st.probe_proxyd()
        assert res.passed is False
        assert "no HTTP response" in res.detail


# ── The cutover footgun: https URL hitting a plaintext listener ───────


class TestCutoverFootgun:
    def test_https_to_plaintext_fails_fast_with_hint(
        self, monkeypatch: pytest.MonkeyPatch, plaintext_proxyd: str
    ) -> None:
        """Pointing an https:// URL at a still-plaintext :4040 must fail the
        handshake (not hang) and surface a debuggable hint about the port."""
        plain = plaintext_proxyd  # http://127.0.0.1:<port>
        https_url = "https://" + plain.split("://", 1)[1]
        monkeypatch.setenv(proxyd.URL_ENV, https_url)
        monkeypatch.delenv(proxyd.INSECURE_ENV, raising=False)
        res = st.probe_proxyd()
        assert res.passed is False
        # The hint names the cutover so the operator reaches for the port, not the cert.
        assert ":4041" in res.detail or "PLAINTEXT" in res.detail

    def test_describe_transport_error_explains_wrong_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(proxyd.URL_ENV, "https://127.0.0.1:4040")
        err = ssl.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
        hint = proxyd.describe_transport_error(err)
        assert "PLAINTEXT" in hint and ":4041" in hint

    def test_describe_transport_error_passthrough_on_plain_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No https → no cutover hint; the raw error is returned unchanged.
        monkeypatch.setenv(proxyd.URL_ENV, "http://127.0.0.1:4040")
        err = OSError("connection refused")
        assert proxyd.describe_transport_error(err) == "connection refused"
