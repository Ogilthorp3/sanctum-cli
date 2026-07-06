"""Unit tests for the mesh HTTP discovery transport + loopback tracker server.

Three layers, each hermetic (no socket, no network):

* :class:`~sanctum_cli.mesh.tracker.TrackerRegistry` — the pure source of truth,
  driven with plain calls (register dedup, announce stores manifest + seeders,
  find hit/miss, catalog/peers round-trip).
* :class:`~sanctum_cli.mesh.tracker.HttpTrackerTransport` — the client, driven
  against an in-memory ``httpx.MockTransport`` whose handler is backed by a real
  ``TrackerRegistry``. This exercises the REAL client HTTP/JSON path: register
  ack True/False, announce body, find hit/miss, peers/catalog round-trip, and
  the honest-verify raises (a 500 or a transport error → ``LocalError``).
* the server — ``build_tracker_app`` route presence + each async handler awaited
  with a stub request (``asyncio.run``; the project ships no ``pytest-asyncio``),
  so the handler→registry glue is covered without binding a port.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from aiohttp import web

from sanctum_cli.errors import LocalError
from sanctum_cli.mesh import artifact
from sanctum_cli.mesh.adapters import Ed25519Signer
from sanctum_cli.mesh.tracker import (
    CommunityOutcome,
    HttpTrackerTransport,
    TrackerHandlers,
    TrackerRegistry,
    build_tracker_app,
)
from sanctum_cli.mesh.types import ArtifactKind, ChampionManifest, MeshIdentity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_BASE = "http://tracker.test"
_INVITE = "https://signal.group/#CjQKIABCDEF"


# ─── shared fixtures / helpers ───────────────────────────────────────────


def _manifest(
    content_hash: str = "sha256:abc", producer_pubkey: str = "ed25519:PUB"
) -> ChampionManifest:
    return ChampionManifest(
        content_hash=content_hash,
        kind=ArtifactKind.LORA_ADAPTER,
        base_model="qwen3.6-35b-a3b-4bit",
        eval_scores={"tiered": 0.897},
        size_bytes=42_000_000,
        producer_pubkey=producer_pubkey,
        signature="sig:XYZ",
    )


def _identity() -> MeshIdentity:
    return MeshIdentity(pubkey="ed25519:PUB", label="manoir", created="2026-07-05T00:00:00Z")


def _mint() -> tuple[Ed25519Signer, str, str]:
    """Return ``(signer, public_hex, private_hex)`` — a real Ed25519 identity."""
    signer = Ed25519Signer()
    public, private = signer.generate()
    return signer, public, private


def _sign_challenge(signer: Ed25519Signer, private: str, public: str, ts: str) -> str:
    """Sign the exact ``community-request:<pubkey>:<ts>`` challenge the gate rebuilds."""
    return signer.sign(private, f"community-request:{public}:{ts}".encode())


def _self_signed_manifest(
    signer: Ed25519Signer, public: str, private: str, *, content_hash: str = "sha256:comm"
) -> ChampionManifest:
    """A manifest REALLY self-signed by ``public`` — passes the announce-credit gate.

    The signature covers the exact bytes :func:`artifact.verify_signature` checks
    (``content_hash`` + the canonical manifest with the signature field stripped),
    so ``verify_signature(manifest, signer.verify)`` holds and ``announce`` credits
    the producer as a contributor.
    """
    base = _manifest(content_hash=content_hash, producer_pubkey=public)
    signature = signer.sign(private, artifact._signing_message(base))
    signed = replace(base, signature=signature)
    assert artifact.verify_signature(signed, signer.verify)  # guard the helper itself
    return signed


def _seeded_registry(
    signer: Ed25519Signer, public: str, private: str, *, signal_invite: str | None = None
) -> TrackerRegistry:
    """A registry where ``public`` has announced one SELF-SIGNED champion (a contributor)."""
    reg = TrackerRegistry(signal_invite=signal_invite)
    reg.announce(_self_signed_manifest(signer, public, private), "100.64.0.1")
    return reg


def _registry_handler(registry: TrackerRegistry) -> Callable[[httpx.Request], httpx.Response]:
    """An ``httpx.MockTransport`` handler that mirrors the real server's wire contract.

    It routes each request straight into ``registry`` and JSON-encodes the answer
    exactly as the aiohttp server does, so the client is driven over the true
    HTTP/JSON path with zero network.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/register":
            body = json.loads(request.content)
            ok = registry.register(body["pubkey"], body["label"], body["created"], body["addr"])
            return httpx.Response(200, json={"ok": ok})
        if path == "/announce":
            body = json.loads(request.content)
            registry.announce(ChampionManifest.from_dict(body["manifest"]), body["addr"])
            return httpx.Response(200, json={"ok": True})
        if path == "/peers":
            return httpx.Response(200, json={"peers": registry.list_peers()})
        if path == "/catalog":
            return httpx.Response(
                200, json={"champions": [m.to_dict() for m in registry.list_catalog()]}
            )
        if path == "/find":
            ref = registry.find(request.url.params.get("hash", ""))
            if ref is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(
                200,
                json={
                    "content_hash": ref.content_hash,
                    "seeders": ref.seeders,
                    "manifest": ref.manifest.to_dict(),
                },
            )
        return httpx.Response(404, json={"error": "unknown route"})

    return handle


@pytest.fixture
def transport_factory() -> Iterator[Callable[..., HttpTrackerTransport]]:
    """Build ``HttpTrackerTransport``s over injected MockTransport clients, closing them.

    Closing every client is load-bearing: the suite runs under
    ``filterwarnings=["error"]``, so a leaked client's ResourceWarning would fail
    the run.
    """
    clients: list[httpx.Client] = []

    def _make(
        registry: TrackerRegistry | None = None,
        *,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> HttpTrackerTransport:
        route = handler if handler is not None else _registry_handler(registry or TrackerRegistry())
        client = httpx.Client(transport=httpx.MockTransport(route))
        clients.append(client)
        return HttpTrackerTransport(_BASE, client=client)

    yield _make
    for client in clients:
        client.close()


# ─── TrackerRegistry — pure, no I/O ──────────────────────────────────────


def test_registry_register_acks_and_adds_peer() -> None:
    reg = TrackerRegistry()
    assert reg.register("pk", "manoir", "2026-07-05", "100.64.0.1") is True
    assert reg.list_peers() == ["100.64.0.1"]


def test_registry_register_dedups_addr() -> None:
    reg = TrackerRegistry()
    reg.register("pk", "manoir", "t", "100.64.0.1")
    reg.register("pk", "manoir", "t", "100.64.0.1")
    reg.register("pk", "manoir", "t", "100.64.0.2")
    assert reg.list_peers() == ["100.64.0.1", "100.64.0.2"]


def test_registry_announce_stores_manifest_and_seeder() -> None:
    reg = TrackerRegistry()
    manifest = _manifest("sha256:x")
    reg.announce(manifest, "100.64.0.5")

    assert reg.list_catalog() == [manifest]
    ref = reg.find("sha256:x")
    assert ref is not None
    assert ref.content_hash == "sha256:x"
    assert ref.seeders == ["100.64.0.5"]
    assert ref.manifest == manifest
    # announcing also makes the node a peer
    assert reg.list_peers() == ["100.64.0.5"]


def test_registry_announce_dedups_seeders_and_accumulates_distinct() -> None:
    reg = TrackerRegistry()
    manifest = _manifest("sha256:x")
    reg.announce(manifest, "100.64.0.5")
    reg.announce(manifest, "100.64.0.5")
    reg.announce(manifest, "100.64.0.6")

    ref = reg.find("sha256:x")
    assert ref is not None
    assert ref.seeders == ["100.64.0.5", "100.64.0.6"]
    assert reg.list_peers() == ["100.64.0.5", "100.64.0.6"]


def test_registry_find_miss_returns_none() -> None:
    assert TrackerRegistry().find("sha256:nope") is None


def test_registry_catalog_lists_all_announced() -> None:
    reg = TrackerRegistry()
    reg.announce(_manifest("sha256:a"), "100.64.0.1")
    reg.announce(_manifest("sha256:b"), "100.64.0.2")
    assert {m.content_hash for m in reg.list_catalog()} == {"sha256:a", "sha256:b"}


def test_registry_list_peers_returns_copy() -> None:
    reg = TrackerRegistry()
    reg.register("pk", "l", "t", "100.64.0.1")
    peers = reg.list_peers()
    peers.append("100.64.0.99")  # mutating the copy must not leak back in
    assert reg.list_peers() == ["100.64.0.1"]


# ─── TrackerRegistry.community — the signed contribute-to-join gate ───────


def test_registry_announce_credits_producer_on_valid_self_signature() -> None:
    # Fix 1: a validly self-signed announce CREDITS the producer as a contributor.
    signer, public, private = _mint()
    reg = TrackerRegistry()
    assert reg.has_seeded(public) is False
    reg.announce(_self_signed_manifest(signer, public, private, content_hash="sha256:x"), "1.1.1.1")
    assert reg.has_seeded(public) is True


def test_registry_announce_forged_signature_is_stored_but_not_credited() -> None:
    # Fix 1: the tracker stays a dumb pointer — a forged/unsigned manifest is still
    # STORED + FINDABLE, but it does NOT credit the announcer as a contributor.
    _signer, public, _private = _mint()
    reg = TrackerRegistry()
    forged = _manifest("sha256:forge", producer_pubkey=public)  # bogus "sig:XYZ"
    reg.announce(forged, "100.64.0.1")

    # stored + findable (discovery is unaffected) …
    assert reg.list_catalog() == [forged]
    ref = reg.find("sha256:forge")
    assert ref is not None
    assert ref.seeders == ["100.64.0.1"]
    # … but NOT credited: the producer never earns the contributor bit.
    assert reg.has_seeded(public) is False


def test_registry_community_not_a_contributor_after_forged_announce() -> None:
    # Fix 1 end-to-end: a forged announce cannot buy the community invite — even a
    # PROVEN owner is refused not_a_contributor because the announce did not credit.
    signer, public, private = _mint()
    reg = TrackerRegistry(signal_invite=_INVITE)
    reg.announce(_manifest("sha256:forge", producer_pubkey=public), "100.64.0.1")  # forged sig
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)

    outcome = reg.community(public, ts, sig)

    assert outcome == CommunityOutcome(invite=None, reason="not_a_contributor")


def test_registry_community_returns_invite_for_seeded_signed_fresh() -> None:
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=_INVITE)
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)

    outcome = reg.community(public, ts, sig)

    assert outcome == CommunityOutcome(invite=_INVITE, reason="ok")


def test_registry_community_bad_signature_even_for_seeded_pubkey() -> None:
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=_INVITE)
    ts = datetime.now(UTC).isoformat()
    # A signature over a DIFFERENT message: the pubkey seeded, but ownership
    # is not proven for this challenge → reveal-nothing bad_signature.
    forged = signer.sign(private, b"community-request:someone-else:2000-01-01T00:00:00+00:00")

    outcome = reg.community(public, ts, forged)

    assert outcome == CommunityOutcome(invite=None, reason="bad_signature")


def test_registry_community_bad_signature_hides_configured_and_contributor() -> None:
    # Fail-closed ordering: an unproven caller must not learn "configured" or
    # "not a contributor" — a bad sig is caught first, before either check.
    signer, public, private = _mint()
    other_signer, _other_pub, other_priv = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=_INVITE)
    ts = datetime.now(UTC).isoformat()
    # Signed by the WRONG key → verify(public, …) fails.
    wrong_key_sig = _sign_challenge(other_signer, other_priv, public, ts)

    outcome = reg.community(public, ts, wrong_key_sig)

    assert outcome.reason == "bad_signature"
    assert outcome.invite is None


def test_registry_community_not_a_contributor_for_never_seeded() -> None:
    signer, public, private = _mint()
    reg = TrackerRegistry(signal_invite=_INVITE)  # nobody has seeded
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)

    outcome = reg.community(public, ts, sig)

    assert outcome == CommunityOutcome(invite=None, reason="not_a_contributor")


def test_registry_community_proven_non_contributor_does_not_leak_not_configured() -> None:
    # Fix 3 (reveal order): a PROVEN, fresh caller who is NOT a contributor must be
    # told not_a_contributor even when NO invite is configured — it must not learn
    # whether a community exists (contributor check precedes config-existence).
    signer, public, private = _mint()
    reg = TrackerRegistry(signal_invite=None)  # proven, but never seeded AND no invite
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)

    outcome = reg.community(public, ts, sig)

    assert outcome == CommunityOutcome(invite=None, reason="not_a_contributor")


def test_registry_community_not_configured_when_invite_unset() -> None:
    # A PROVEN contributor on a registry with no invite learns not_configured.
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=None)  # seeded, no invite
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)

    outcome = reg.community(public, ts, sig)

    assert outcome == CommunityOutcome(invite=None, reason="not_configured")


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_registry_community_empty_or_whitespace_invite_is_not_configured(blank: str) -> None:
    # Fix 4: an empty/whitespace-only invite is normalized to "no community" →
    # a proven contributor gets not_configured (never an empty invite string).
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=blank)
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)

    outcome = reg.community(public, ts, sig)

    assert outcome == CommunityOutcome(invite=None, reason="not_configured")


def test_registry_community_stale_when_now_far_in_future() -> None:
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=_INVITE)
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)  # a real, valid signature
    far_future = datetime.now(UTC) + timedelta(hours=1)

    outcome = reg.community(public, ts, sig, now=far_future)

    assert outcome == CommunityOutcome(invite=None, reason="stale")


def test_registry_community_stale_on_malformed_ts_fail_closed() -> None:
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=_INVITE)
    ts = "not-a-timestamp"
    sig = _sign_challenge(signer, private, public, ts)  # signature is valid…

    outcome = reg.community(public, ts, sig)

    # …but the challenge instant is unparseable → fail-closed as stale.
    assert outcome == CommunityOutcome(invite=None, reason="stale")


def test_registry_community_fresh_within_skew_window_is_ok() -> None:
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=_INVITE)
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)
    just_inside = datetime.now(UTC) + timedelta(seconds=90)  # < ±120 s

    outcome = reg.community(public, ts, sig, now=just_inside)

    assert outcome.reason == "ok"


# ─── HttpTrackerTransport — REAL client HTTP/JSON over MockTransport ──────


def test_client_register_returns_true_ack(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    reg = TrackerRegistry()
    transport = transport_factory(reg)
    assert transport.register(_identity(), "100.64.0.1") is True
    # the request really reached the registry
    assert reg.list_peers() == ["100.64.0.1"]


def test_client_register_returns_false_when_tracker_declines(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def decline(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False})

    transport = transport_factory(handler=decline)
    assert transport.register(_identity(), "100.64.0.1") is False


def test_client_register_raises_on_non_bool_ok(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def weird(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": "yes"})

    transport = transport_factory(handler=weird)
    with pytest.raises(LocalError):
        transport.register(_identity(), "100.64.0.1")


def test_client_announce_posts_manifest_and_addr(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    transport = transport_factory(handler=capture)
    manifest = _manifest("sha256:x")
    transport.announce(manifest, "100.64.0.7")

    assert seen["path"] == "/announce"
    assert seen["body"] == {"manifest": manifest.to_dict(), "addr": "100.64.0.7"}


def test_client_announce_then_find_round_trips_through_registry(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    reg = TrackerRegistry()
    transport = transport_factory(reg)
    manifest = _manifest("sha256:x")

    transport.announce(manifest, "100.64.0.7")
    ref = transport.find("sha256:x")

    assert ref is not None
    assert ref.content_hash == "sha256:x"
    assert ref.seeders == ["100.64.0.7"]
    assert ref.manifest == manifest  # from_dict/to_dict round-trip is exact


def test_client_find_miss_returns_none_on_404(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    transport = transport_factory(TrackerRegistry())
    assert transport.find("sha256:absent") is None


def test_client_peers_round_trips(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    reg = TrackerRegistry()
    reg.register("pk", "l", "t", "100.64.0.1")
    reg.register("pk", "l", "t", "100.64.0.2")
    transport = transport_factory(reg)
    assert transport.peers() == ["100.64.0.1", "100.64.0.2"]


def test_client_catalog_round_trips_manifests(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    reg = TrackerRegistry()
    reg.announce(_manifest("sha256:a"), "100.64.0.1")
    reg.announce(_manifest("sha256:b"), "100.64.0.2")
    transport = transport_factory(reg)

    champions = transport.catalog()
    assert {m.content_hash for m in champions} == {"sha256:a", "sha256:b"}
    assert all(isinstance(m, ChampionManifest) for m in champions)


# ─── HttpTrackerTransport — honest-verify: a down tracker RAISES ─────────


@pytest.mark.parametrize("status", [500, 502, 503])
def test_client_raises_localerror_on_5xx(
    transport_factory: Callable[..., HttpTrackerTransport], status: int
) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "kaboom"})

    transport = transport_factory(handler=boom)
    with pytest.raises(LocalError):
        transport.peers()


def test_client_find_raises_on_5xx_not_returns_none(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    # The critical honest-verify case: a broken tracker must never look like a miss.
    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "kaboom"})

    transport = transport_factory(handler=boom)
    with pytest.raises(LocalError):
        transport.find("sha256:x")


def test_client_raises_localerror_on_transport_error(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = transport_factory(handler=unreachable)
    with pytest.raises(LocalError):
        transport.peers()


def test_client_announce_raises_on_5xx(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "kaboom"})

    transport = transport_factory(handler=boom)
    with pytest.raises(LocalError):
        transport.announce(_manifest("sha256:x"), "100.64.0.1")


def test_client_raises_on_non_json_body(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def garbage(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    transport = transport_factory(handler=garbage)
    with pytest.raises(LocalError):
        transport.peers()


# ─── HttpTrackerTransport.community — the client leg of the gate ─────────


def test_client_community_returns_invite_on_200(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"invite": _INVITE})

    transport = transport_factory(handler=handler)
    assert transport.community("pub", "ts", "sig") == CommunityOutcome(invite=_INVITE, reason="ok")


def test_client_community_posts_pubkey_ts_signature_body(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    # Fix 2 (client leg): the signed credential is POSTed in the BODY — never a
    # URL query — so it cannot leak into access logs / referrers.
    seen: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"invite": _INVITE})

    transport = transport_factory(handler=capture)
    transport.community("PUB", "TS", "SIG")

    assert seen["method"] == "POST"
    assert seen["path"] == "/community"
    assert seen["params"] == {}  # nothing in the query string
    assert seen["body"] == {"pubkey": "PUB", "ts": "TS", "signature": "SIG"}


@pytest.mark.parametrize("reason", ["bad_signature", "stale", "not_a_contributor"])
def test_client_community_maps_403_to_typed_refusal(
    transport_factory: Callable[..., HttpTrackerTransport], reason: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"reason": reason})

    transport = transport_factory(handler=handler)
    assert transport.community("pub", "ts", "sig") == CommunityOutcome(invite=None, reason=reason)


def test_client_community_maps_404_to_not_configured(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"reason": "not_configured"})

    transport = transport_factory(handler=handler)
    assert transport.community("pub", "ts", "sig") == CommunityOutcome(
        invite=None, reason="not_configured"
    )


def test_client_community_raises_localerror_on_5xx(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "kaboom"})

    transport = transport_factory(handler=boom)
    with pytest.raises(LocalError):
        transport.community("pub", "ts", "sig")


def test_client_community_raises_on_200_without_invite(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    # Honest-verify: a 200 whose body carries no invite string is a misbehaving
    # tracker, never a fabricated (empty) invite.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"welcome": True})

    transport = transport_factory(handler=handler)
    with pytest.raises(LocalError):
        transport.community("pub", "ts", "sig")


def test_client_community_raises_on_unknown_403_reason(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"reason": "teapot"})

    transport = transport_factory(handler=handler)
    with pytest.raises(LocalError):
        transport.community("pub", "ts", "sig")


def test_client_community_raises_localerror_on_transport_error(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = transport_factory(handler=unreachable)
    with pytest.raises(LocalError):
        transport.community("pub", "ts", "sig")


# ─── HttpTrackerTransport — client lifetime ──────────────────────────────


def test_client_owns_and_closes_internal_client() -> None:
    transport = HttpTrackerTransport(_BASE)
    assert transport._owns_client is True
    assert isinstance(transport._client, httpx.Client)
    transport.close()  # closing an owned client must not raise or warn


def test_client_context_manager_closes_owned_client() -> None:
    with HttpTrackerTransport(_BASE) as transport:
        assert transport._owns_client is True
    assert transport._client.is_closed


def test_injected_client_is_not_owned() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    transport = HttpTrackerTransport(_BASE, client=client)
    assert transport._owns_client is False
    transport.close()  # no-op for an injected client
    assert client.is_closed is False
    client.close()


def test_base_url_trailing_slash_is_stripped() -> None:
    transport = HttpTrackerTransport(_BASE + "/")
    assert transport._base == _BASE
    transport.close()


# ─── server: build_tracker_app route presence ───────────────────────────


def test_build_tracker_app_returns_application() -> None:
    assert isinstance(build_tracker_app(), web.Application)


def test_build_tracker_app_wires_all_routes() -> None:
    app = build_tracker_app()
    # aiohttp auto-adds a HEAD route for each GET; filter those out.
    routes = {
        (route.method, route.resource.canonical)  # type: ignore[union-attr]
        for route in app.router.routes()
        if route.method != "HEAD"
    }
    assert routes == {
        ("POST", "/register"),
        ("GET", "/peers"),
        ("GET", "/catalog"),
        ("POST", "/announce"),
        ("GET", "/find"),
        ("POST", "/community"),
    }


# ─── server: async handler glue (asyncio.run — no pytest-asyncio) ────────


class _StubRequest:
    """A minimal stand-in for ``web.Request`` covering what the handlers read."""

    def __init__(
        self, *, json_body: dict[str, Any] | None = None, query: dict[str, str] | None = None
    ) -> None:
        self._json_body = json_body
        self.query = dict(query or {})

    async def json(self) -> dict[str, Any]:
        assert self._json_body is not None
        return self._json_body


def _body(response: web.Response) -> Any:
    assert response.body is not None
    return json.loads(response.body)


def test_handler_register_delegates_to_registry() -> None:
    reg = TrackerRegistry()
    handlers = TrackerHandlers(reg)
    req = _StubRequest(
        json_body={"pubkey": "pk", "label": "l", "created": "t", "addr": "100.64.0.1"}
    )
    response = asyncio.run(handlers.register(req))  # type: ignore[arg-type]

    assert response.status == 200
    assert _body(response) == {"ok": True}
    assert reg.list_peers() == ["100.64.0.1"]


def test_handler_announce_stores_then_find_returns_it() -> None:
    reg = TrackerRegistry()
    handlers = TrackerHandlers(reg)
    manifest = _manifest("sha256:x")

    announce_resp = asyncio.run(
        handlers.announce(
            _StubRequest(json_body={"manifest": manifest.to_dict(), "addr": "100.64.0.9"}),  # type: ignore[arg-type]
        )
    )
    assert announce_resp.status == 200

    find_resp = asyncio.run(handlers.find(_StubRequest(query={"hash": "sha256:x"})))  # type: ignore[arg-type]
    assert find_resp.status == 200
    payload = _body(find_resp)
    assert payload["content_hash"] == "sha256:x"
    assert payload["seeders"] == ["100.64.0.9"]
    assert ChampionManifest.from_dict(payload["manifest"]) == manifest


def test_handler_find_miss_returns_404() -> None:
    handlers = TrackerHandlers(TrackerRegistry())
    response = asyncio.run(handlers.find(_StubRequest(query={"hash": "sha256:absent"})))  # type: ignore[arg-type]
    assert response.status == 404


def test_handler_peers_and_catalog_report_registry_state() -> None:
    reg = TrackerRegistry()
    reg.register("pk", "l", "t", "100.64.0.1")
    reg.announce(_manifest("sha256:a"), "100.64.0.2")
    handlers = TrackerHandlers(reg)

    peers_resp = asyncio.run(handlers.peers(_StubRequest()))  # type: ignore[arg-type]
    assert set(_body(peers_resp)["peers"]) == {"100.64.0.1", "100.64.0.2"}

    catalog_resp = asyncio.run(handlers.catalog(_StubRequest()))  # type: ignore[arg-type]
    champions = _body(catalog_resp)["champions"]
    assert [c["content_hash"] for c in champions] == ["sha256:a"]


# ─── server: /community handler glue (real Ed25519 round-trip) ───────────


def test_handler_community_ok_returns_200_invite() -> None:
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=_INVITE)
    handlers = TrackerHandlers(reg)
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)

    resp = asyncio.run(
        handlers.community(  # type: ignore[arg-type]
            _StubRequest(json_body={"pubkey": public, "ts": ts, "signature": sig})
        )
    )

    assert resp.status == 200
    assert _body(resp) == {"invite": _INVITE}


def test_handler_community_bad_signature_returns_403() -> None:
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=_INVITE)
    handlers = TrackerHandlers(reg)
    ts = datetime.now(UTC).isoformat()

    resp = asyncio.run(
        handlers.community(  # type: ignore[arg-type]
            _StubRequest(json_body={"pubkey": public, "ts": ts, "signature": "00"})
        )
    )

    assert resp.status == 403
    assert _body(resp) == {"reason": "bad_signature"}


def test_handler_community_not_configured_returns_404() -> None:
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=None)
    handlers = TrackerHandlers(reg)
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)

    resp = asyncio.run(
        handlers.community(  # type: ignore[arg-type]
            _StubRequest(json_body={"pubkey": public, "ts": ts, "signature": sig})
        )
    )

    assert resp.status == 404
    assert _body(resp) == {"reason": "not_configured"}


def test_handler_community_reads_signed_body_end_to_end() -> None:
    # The handler reads {pubkey, ts, signature} from the POST body (not the query)
    # and proves ownership end to end with the registry's default Ed25519 verify.
    signer, public, private = _mint()
    reg = _seeded_registry(signer, public, private, signal_invite=_INVITE)
    handlers = TrackerHandlers(reg)
    ts = datetime.now(UTC).isoformat()
    sig = _sign_challenge(signer, private, public, ts)

    resp = asyncio.run(
        handlers.community(  # type: ignore[arg-type]
            _StubRequest(json_body={"pubkey": public, "ts": ts, "signature": sig})
        )
    )

    assert resp.status == 200
    assert _body(resp) == {"invite": _INVITE}


# ─── malformed manifests from the OPEN (untrusted) tracker → clean raise ─────

_BAD_MANIFEST = {
    "content_hash": "sha256:" + "a" * 64,
    "kind": "evil_kind",  # not a valid ArtifactKind → ValueError in from_dict
    "base_model": "m",
    "eval_scores": {},
    "size_bytes": 1,
    "producer_pubkey": "pk",
    "signature": "sig",
}


def test_catalog_raises_localerror_on_malformed_manifest(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    # The tracker is untrusted; a bad manifest (invalid `kind` enum) must surface
    # as a clean LocalError, never a raw ValueError leaking past the CLI.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"champions": [_BAD_MANIFEST]})

    client = transport_factory(handler=handler)
    with pytest.raises(LocalError):
        client.catalog()


def test_find_raises_localerror_on_malformed_manifest(
    transport_factory: Callable[..., HttpTrackerTransport],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content_hash": _BAD_MANIFEST["content_hash"],
                "seeders": ["100.64.0.1"],
                "manifest": _BAD_MANIFEST,
            },
        )

    client = transport_factory(handler=handler)
    with pytest.raises(LocalError):
        client.find(str(_BAD_MANIFEST["content_hash"]))
