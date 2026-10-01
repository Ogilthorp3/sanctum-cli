"""Named smoke: a friend joins the tribe, then receives a signed file.

Run: python3 -m pytest tests/mesh/test_tribe_smoke.py
"""

from __future__ import annotations

import pytest

from sanctum_cli.commands.mesh import guard_tribe_join, tribe_up_argv
from sanctum_cli.errors import UserError
from sanctum_cli.mesh.adapters import Ed25519Signer
from sanctum_cli.mesh.files import (
    ParcelServer,
    build_file_parcel,
    encode_ticket,
    receive_parcel,
    url_is_allowed,
)
from sanctum_cli.mesh.identity import MeshIdentityStore


def test_haus_node_cannot_join_the_tribe() -> None:
    with pytest.raises(UserError, match="haus node"):
        guard_tribe_join(["tag:sanctum-admin"])


def test_tribe_join_advertises_only_the_tribe_tag() -> None:
    argv = tribe_up_argv("tskey-auth-example", "Godot's Studio")
    assert argv[0] == "tailscale"
    assert "--advertise-tags" in argv
    assert argv[argv.index("--advertise-tags") + 1] == "tag:sanctum-tribe"
    assert "tag:sanctum-admin" not in argv
    assert argv[argv.index("--hostname") + 1] == "godots-studio"


def test_tribe_peer_receives_a_signed_file(tmp_path) -> None:
    """The tailnet address is the ticket. The bytes are checked before they are kept."""
    source = tmp_path / "sanctum-studio.zip"
    source.write_bytes(b"tribe-parcel")
    identity = MeshIdentityStore(Ed25519Signer(), path=tmp_path / "id").ensure("operator")
    parcel = build_file_parcel(source, identity)
    server = ParcelServer(source, parcel, "127.0.0.1", 0)
    server.start()
    try:
        # ip-allow: Tailscale CGNAT example, not a host address
        peer = "100.99.0.8"
        assert url_is_allowed(f"http://{peer}:8788/v1/file")
        ticket = encode_ticket(parcel, [server.url])
        saved = receive_parcel(ticket, tmp_path / "inbox", Ed25519Signer().verify)
    finally:
        server.stop()
    assert saved.read_bytes() == b"tribe-parcel"
    assert saved.name == "sanctum-studio.zip"
