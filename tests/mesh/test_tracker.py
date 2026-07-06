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
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from aiohttp import web

from sanctum_cli.errors import LocalError
from sanctum_cli.mesh.tracker import (
    HttpTrackerTransport,
    TrackerHandlers,
    TrackerRegistry,
    build_tracker_app,
)
from sanctum_cli.mesh.types import ArtifactKind, ChampionManifest, MeshIdentity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_BASE = "http://tracker.test"


# ─── shared fixtures / helpers ───────────────────────────────────────────


def _manifest(content_hash: str = "sha256:abc") -> ChampionManifest:
    return ChampionManifest(
        content_hash=content_hash,
        kind=ArtifactKind.LORA_ADAPTER,
        base_model="qwen3.6-35b-a3b-4bit",
        eval_scores={"tiered": 0.897},
        size_bytes=42_000_000,
        producer_pubkey="ed25519:PUB",
        signature="sig:XYZ",
    )


def _identity() -> MeshIdentity:
    return MeshIdentity(pubkey="ed25519:PUB", label="manoir", created="2026-07-05T00:00:00Z")


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


def test_build_tracker_app_wires_the_five_routes() -> None:
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
