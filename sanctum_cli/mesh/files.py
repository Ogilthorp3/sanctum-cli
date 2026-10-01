"""File parcels on the Sanctum mesh.

A parcel is one file, content-addressed and signed by the sender's mesh
identity. The receiver checks the signature and the sha256, then writes the
file into an inbox. It does not run the champion eval or the sandbox, and it
does not execute the file.

The ticket is a one-line capability: the signed description plus the URLs
where the bytes are being served. The bytes stay on the sender until the
receiver fetches them.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from sanctum_cli.errors import LocalError, NetworkError, SanctumError, UserError
from sanctum_cli.mesh.artifact import content_hash

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sanctum_cli.mesh.artifact import SigningIdentity, VerifyFn

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# ip-allow: Tailscale CGNAT range, not a host address
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
_CHUNK = 1 << 16


@dataclass(frozen=True)
class FileParcel:
    """Signed description of one file. The bytes are not in here."""

    content_hash: str
    filename: str
    size_bytes: int
    producer_pubkey: str
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "producer_pubkey": self.producer_pubkey,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileParcel:
        return cls(
            content_hash=str(data["content_hash"]),
            filename=str(data["filename"]),
            size_bytes=int(data["size_bytes"]),
            producer_pubkey=str(data["producer_pubkey"]),
            signature=str(data["signature"]),
        )


def safe_filename(name: str) -> str:
    """Return ``name`` if it is a single safe path segment, else raise."""
    if not _NAME.fullmatch(name):
        raise UserError(
            f"unsafe parcel name {name!r}",
            fix="use a file name of letters, numbers, dots, dashes, and underscores",
        )
    return name


def signing_message(parcel: FileParcel) -> bytes:
    """The exact bytes the sender signs and the receiver verifies."""
    return (
        "sanctum-mesh-file\n"
        f"{parcel.content_hash}\n"
        f"{parcel.filename}\n"
        f"{parcel.size_bytes}\n"
        f"{parcel.producer_pubkey}\n"
    ).encode()


def build_file_parcel(path: Path, identity: SigningIdentity) -> FileParcel:
    """Hash and sign ``path``. The stored name is the file's own basename."""
    if not path.is_file():
        raise UserError(
            f"not a file: {path}",
            fix="pass the package file, for example sanctum-studio.zip",
        )
    filename = safe_filename(path.name)
    unsigned = FileParcel(
        content_hash=content_hash(path),
        filename=filename,
        size_bytes=path.stat().st_size,
        producer_pubkey=identity.pubkey,
        signature="",
    )
    signature = identity.sign(signing_message(unsigned))
    return FileParcel(
        content_hash=unsigned.content_hash,
        filename=unsigned.filename,
        size_bytes=unsigned.size_bytes,
        producer_pubkey=unsigned.producer_pubkey,
        signature=signature,
    )


def encode_ticket(parcel: FileParcel, urls: list[str]) -> str:
    """One-line ticket. The file bytes are not included."""
    payload = parcel.to_dict()
    payload["urls"] = list(urls)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_ticket(ticket: str) -> tuple[FileParcel, list[str]]:
    """Rebuild a parcel and its fetch URLs from :func:`encode_ticket`."""
    text = ticket.strip()
    if not text:
        raise UserError("empty ticket", fix="paste the line from sanctum mesh send")
    pad = "=" * (-len(text) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(text + pad))
        parcel = FileParcel.from_dict(data)
        urls = [str(item) for item in data["urls"]]
    except (KeyError, ValueError, json.JSONDecodeError, TypeError) as exc:
        raise UserError(
            "ticket is not a mesh file parcel",
            fix="paste the whole line from sanctum mesh send",
        ) from exc
    safe_filename(parcel.filename)
    if not urls:
        raise UserError("ticket has no fetch address", fix="ask the sender to run send again")
    return parcel, urls


def url_is_allowed(url: str) -> bool:
    """Allow loopback, private, and Tailscale HTTP, plus any HTTPS URL.

    Plain HTTP to a public address is refused so the package is not sent
    across the open internet without TLS. Link-local addresses are refused
    so a ticket cannot point the receiver at the cloud metadata address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    host = parsed.hostname
    if host is None:
        return False
    if host.lower() == "localhost":
        return parsed.scheme == "http"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return parsed.scheme == "https"
    if address.is_loopback:
        return parsed.scheme == "http"
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        return False
    if address in _TAILSCALE or address.is_private:
        return parsed.scheme == "http"
    if address.is_reserved:
        return False
    return parsed.scheme == "https"


class ParcelServer:
    """Serve one parcel's bytes on a single address until :meth:`stop`."""

    def __init__(self, path: Path, parcel: FileParcel, host: str, port: int) -> None:
        self.path = path
        self.parcel = parcel
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        if self._httpd is None:
            raise LocalError("parcel server is not started")
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.bound_port}/v1/file"

    def start(self) -> None:
        path = self.path
        expected = self.parcel.size_bytes

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] != "/v1/file":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(expected))
                self.end_headers()
                with path.open("rb") as handle:
                    while True:
                        chunk = handle.read(_CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def serve_until_stopped(servers: list[ParcelServer]) -> None:
    """Block until Ctrl-C, then stop every server."""
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return
    finally:
        for server in servers:
            server.stop()


def tailscale_ipv4(run: Callable[[list[str]], str]) -> str | None:
    """Return this node's Tailscale IPv4, or None when Tailscale is down."""
    text = run(["tailscale", "ip", "-4"])
    for line in text.splitlines():
        candidate = line.strip()
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address in _TAILSCALE:
            return candidate
    return None


def receive_parcel(
    ticket: str,
    inbox: Path,
    verify: VerifyFn,
    *,
    client: httpx.Client | None = None,
) -> Path:
    """Fetch a ticket, check the signature and the hash, and save the file.

    Returns the path written under ``inbox``. A bad signature is rejected
    before any byte is kept. A hash mismatch deletes the partial download.
    """
    parcel, urls = decode_ticket(ticket)
    if not verify(parcel.producer_pubkey, signing_message(parcel), parcel.signature):
        raise LocalError(
            "parcel signature does not verify",
            fix="ask the sender to run sanctum mesh send again",
        )
    allowed = [url for url in urls if url_is_allowed(url)]
    if not allowed:
        raise UserError(
            "ticket addresses are not reachable from this mesh mode",
            fix="the sender must be on this tailnet, or serve the parcel over HTTPS",
        )
    inbox.mkdir(parents=True, exist_ok=True)
    destination = inbox / parcel.filename
    if destination.exists():
        raise LocalError(
            f"{destination} already exists",
            fix="move that file aside, then receive again",
        )
    own_client = client is None
    http = client if client is not None else httpx.Client(follow_redirects=False, timeout=120.0)
    try:
        last: SanctumError | None = None
        for url in allowed:
            try:
                return _fetch(http, url, parcel, destination)
            except (NetworkError, LocalError) as exc:
                last = exc
        if last is None:
            raise LocalError("parcel was not fetched")
        raise last
    finally:
        if own_client:
            http.close()


def _fetch(http: httpx.Client, url: str, parcel: FileParcel, destination: Path) -> Path:
    partial = destination.with_suffix(destination.suffix + ".partial")
    digest = hashlib.sha256()
    written = 0
    try:
        with http.stream("GET", url) as response:
            if response.status_code != 200:
                raise NetworkError(
                    f"sender answered {response.status_code} for the parcel",
                    fix="confirm sanctum mesh send is still running",
                )
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    written += len(chunk)
                    if written > parcel.size_bytes:
                        raise LocalError(
                            "parcel is larger than the signed size",
                            fix="refuse the ticket and ask the sender to send again",
                        )
                    digest.update(chunk)
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise NetworkError(
            f"could not fetch the parcel: {exc}",
            fix="confirm sanctum mesh send is still running and this Mac can reach it",
        ) from exc
    actual = "sha256:" + digest.hexdigest()
    if written != parcel.size_bytes or actual != parcel.content_hash:
        partial.unlink(missing_ok=True)
        raise LocalError(
            "parcel bytes do not match the signed hash",
            fix="refuse the ticket and ask the sender to send again",
        )
    partial.replace(destination)
    return destination
