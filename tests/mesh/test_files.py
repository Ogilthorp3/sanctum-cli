"""File parcels: sign, ticket, serve, receive. No champion eval."""

from __future__ import annotations

import base64
import json

import pytest

from sanctum_cli.errors import LocalError, UserError
from sanctum_cli.mesh.adapters import Ed25519Signer
from sanctum_cli.mesh.files import (
    ParcelServer,
    build_file_parcel,
    decode_ticket,
    encode_ticket,
    receive_parcel,
    tailscale_ipv4,
    url_is_allowed,
)
from sanctum_cli.mesh.identity import MeshIdentityStore


def _identity(tmp_path):
    return MeshIdentityStore(Ed25519Signer(), path=tmp_path / "id").ensure("haus")


def test_round_trip_on_loopback(tmp_path) -> None:
    source = tmp_path / "sanctum-studio.zip"
    source.write_bytes(b"studio-bytes")
    identity = _identity(tmp_path)
    parcel = build_file_parcel(source, identity)
    server = ParcelServer(source, parcel, "127.0.0.1", 0)
    server.start()
    try:
        ticket = encode_ticket(parcel, [server.url])
        saved = receive_parcel(ticket, tmp_path / "inbox", Ed25519Signer().verify)
    finally:
        server.stop()
    assert saved.read_bytes() == b"studio-bytes"
    assert saved.name == "sanctum-studio.zip"


def test_tampered_bytes_are_refused(tmp_path) -> None:
    source = tmp_path / "package.zip"
    source.write_bytes(b"good")
    identity = _identity(tmp_path)
    parcel = build_file_parcel(source, identity)
    source.write_bytes(b"evil")
    server = ParcelServer(source, parcel, "127.0.0.1", 0)
    server.start()
    try:
        ticket = encode_ticket(parcel, [server.url])
        with pytest.raises(LocalError, match="signed hash"):
            receive_parcel(ticket, tmp_path / "inbox", Ed25519Signer().verify)
    finally:
        server.stop()
    assert not (tmp_path / "inbox" / "package.zip").exists()


def test_bad_signature_is_refused_before_fetch(tmp_path) -> None:
    source = tmp_path / "package.zip"
    source.write_bytes(b"good")
    parcel = build_file_parcel(source, _identity(tmp_path))
    forged = parcel.__class__(
        content_hash=parcel.content_hash,
        filename=parcel.filename,
        size_bytes=parcel.size_bytes,
        producer_pubkey=parcel.producer_pubkey,
        signature="00",
    )
    ticket = encode_ticket(forged, ["http://127.0.0.1:9/v1/file"])
    with pytest.raises(LocalError, match="signature"):
        receive_parcel(ticket, tmp_path / "inbox", Ed25519Signer().verify)


def test_public_http_url_is_refused() -> None:
    assert url_is_allowed("http://127.0.0.1:8788/v1/file")
    # ip-allow: Tailscale CGNAT example, not a host address
    assert url_is_allowed("http://100.64.0.2:8788/v1/file")
    assert url_is_allowed("https://example.ts.net/v1/file")
    assert not url_is_allowed("http://8.8.8.8/v1/file")
    assert not url_is_allowed("http://169.254.169.254/latest")
    assert not url_is_allowed("file:///etc/passwd")


def test_unsafe_name_is_refused(tmp_path) -> None:
    source = tmp_path / "ok.zip"
    source.write_bytes(b"x")
    parcel = build_file_parcel(source, _identity(tmp_path))
    raw = parcel.to_dict()
    raw["filename"] = "../passwd"
    raw["urls"] = ["http://127.0.0.1/v1/file"]
    ticket = base64.urlsafe_b64encode(
        json.dumps(raw, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    with pytest.raises(UserError, match="unsafe"):
        decode_ticket(ticket)


def test_tailscale_ipv4_ignores_other_addresses() -> None:
    def run(_argv: list[str]) -> str:
        # ip-allow: Tailscale CGNAT example, not a host address
        return "192.168.1.9\n100.64.1.8\n"

    assert tailscale_ipv4(run) == "100.64.1.8"
    assert tailscale_ipv4(lambda _argv: "") is None
