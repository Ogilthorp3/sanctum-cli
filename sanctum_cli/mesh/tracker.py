"""HTTP discovery transport (client) + a real loopback tracker server.

This is the Layer-1 *tracker* boundary the mesh's injected discovery seam sits
behind. Three concentric pieces, smallest-and-purest first:

* :class:`TrackerRegistry` — a pure, dict-backed source of truth (peers, a
  content-addressed catalog, and per-hash seeders). No ``httpx`` / ``aiohttp``
  import touches it, so every rule (register-dedup, announce-stores-seeders,
  find hit/miss) is unit-tested with plain calls and zero I/O.
* :class:`HttpTrackerTransport` — the *client*. It implements the five
  ``MeshDirectory`` methods (``register`` / ``peers`` / ``catalog`` /
  ``announce`` / ``find``) over HTTP against a :class:`TrackerRegistry`-backed
  server. It is honest-verify to the bone: a down tracker, a 5xx, or an
  unexpected shape **raises** :class:`~sanctum_cli.errors.LocalError` — it never
  launders a transport failure into an empty ``[]`` / ``None`` / ``False``. The
  only non-exceptional "empty" answers are a clean ``404`` on ``find`` (a
  genuine discovery miss → ``None``) and the tracker legitimately answering
  ``{"ok": false}`` on ``register`` (a real "not ack'd" → ``False``).
* :func:`build_tracker_app` / :func:`serve` — the *server*: thin async aiohttp
  handlers that delegate to a :class:`TrackerRegistry` and JSON-encode its
  answers. This is what the single-box e2e drill and a real deployment run; it
  need not stand up a live socket inside ``make check``.

The client is verified against an in-memory ``httpx.MockTransport`` (the real
HTTP/JSON path, zero network); the server's handler→registry glue is verified
by awaiting each handler with a stub request. The two mirror one wire contract,
so a change on one side that the other does not follow shows up as a test break.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from aiohttp import web

from sanctum_cli.errors import LocalError
from sanctum_cli.mesh.adapters import Ed25519Signer
from sanctum_cli.mesh.types import ArtifactRef, ChampionManifest, MeshIdentity

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    # Import-for-typing only: a runtime import would be circular (commands.mesh
    # imports the mesh package). Structural typing makes HttpTrackerTransport a
    # MeshDirectory without any runtime coupling — see ``_conformance`` below.
    from sanctum_cli.commands.mesh import MeshDirectory

    # The Ed25519 verify seam shape: ``(pubkey, message, signature) -> ok``.
    # Injectable so the community gate can be unit-tested with a fake, and so a
    # PQ signer can drop in behind the same shape.
    VerifyFn = Callable[[str, bytes, str], bool]

__all__ = [
    "CommunityOutcome",
    "HttpTrackerTransport",
    "TrackerHandlers",
    "TrackerRegistry",
    "build_tracker_app",
    "serve",
]

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765

# The signed community challenge is fresh within ±120 s of the tracker's clock —
# a small window that bounds replay of a captured request (a tracker-issued nonce
# is the stronger, deferred option).
_COMMUNITY_SKEW_SECONDS = 120.0
# The typed 403 refusals the client will accept back on the wire; anything else
# in a 403 body is a misbehaving tracker (honest-verify raise).
_COMMUNITY_REFUSALS = frozenset({"bad_signature", "stale", "not_a_contributor"})
# 403 must pass through _send (a legitimate refusal body), unlike a 5xx (raise).
_COMMUNITY_ALLOW_STATUS = frozenset({int(httpx.codes.FORBIDDEN)})


@dataclass(frozen=True)
class CommunityOutcome:
    """The tracker's verdict on a ``/community`` request.

    ``invite`` is the Signal group link on success (``reason == "ok"``) and
    ``None`` on every refusal. ``reason`` is one of ``ok`` / ``bad_signature`` /
    ``stale`` / ``not_a_contributor`` / ``not_configured`` — a typed outcome the
    HTTP layer maps to a status code and the CLI maps to an honest message.
    """

    invite: str | None
    reason: str


def _challenge_is_stale(ts: str, now: datetime | None) -> bool:
    """Return whether ``ts`` is outside the ±120 s freshness window (fail-closed).

    ``ts`` must be an unambiguous ISO-8601 UTC instant — a malformed *or*
    timezone-naive timestamp is treated as stale so it cannot slip past the
    replay bound. ``now`` defaults to the tracker's current UTC clock.
    """
    reference = now if now is not None else datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return True
    if parsed.tzinfo is None:
        return True
    return abs((reference - parsed).total_seconds()) > _COMMUNITY_SKEW_SECONDS


# ─── the source of truth (pure, hermetic, no network import) ─────────────


class TrackerRegistry:
    """The tracker's in-memory state — peers, catalog, and per-hash seeders.

    Deliberately dependency-free: neither the client nor the server logic lives
    here, only the data rules. ``peers`` is an insertion-ordered de-duplicated
    list of addrs; ``catalog`` maps ``content_hash -> ChampionManifest``;
    ``seeders`` maps ``content_hash -> [addr, ...]``. Registered node identities
    are kept keyed by addr so a tracker can attribute who is at each address.
    """

    def __init__(self, signal_invite: str | None = None) -> None:
        self._peers: list[str] = []
        self._identities: dict[str, MeshIdentity] = {}
        self._catalog: dict[str, ChampionManifest] = {}
        self._seeders: dict[str, list[str]] = {}
        # Every pubkey that has announced ≥ 1 champion — the contribute-to-join
        # set the community gate reads. ``signal_invite`` is operator config (the
        # Signal group link); ``None`` means no community is set up on this mesh.
        self._seeded_pubkeys: set[str] = set()
        self._signal_invite = signal_invite

    def register(self, pubkey: str, label: str, created: str, addr: str) -> bool:
        """Record ``(pubkey, label, created)`` at ``addr`` and ack (always True).

        Idempotent: re-registering an addr refreshes its identity and never
        duplicates it in ``peers``.
        """
        self._identities[addr] = MeshIdentity(pubkey=pubkey, label=label, created=created)
        self._add_peer(addr)
        return True

    def list_peers(self) -> list[str]:
        """Return a copy of the known peer addrs (insertion order, de-duplicated)."""
        return list(self._peers)

    def list_catalog(self) -> list[ChampionManifest]:
        """Return every advertised champion manifest currently known."""
        return list(self._catalog.values())

    def announce(self, manifest: ChampionManifest, addr: str) -> None:
        """Store ``manifest`` and record ``addr`` as one of its seeders.

        ``addr`` is de-duplicated into both the manifest's seeder set (so ``find``
        reports it) and the global peer list (an announcing node is a peer).
        """
        self._catalog[manifest.content_hash] = manifest
        seeders = self._seeders.setdefault(manifest.content_hash, [])
        if addr not in seeders:
            seeders.append(addr)
        self._add_peer(addr)
        # The producer is now a contributor: it has given a champion to the swarm.
        self._seeded_pubkeys.add(manifest.producer_pubkey)

    def has_seeded(self, pubkey: str) -> bool:
        """Return whether ``pubkey`` has announced at least one champion.

        A pubkey becomes a contributor the instant :meth:`announce` records a
        manifest it produced — the mesh's contribute-to-join gate.
        """
        return pubkey in self._seeded_pubkeys

    def community(
        self,
        pubkey: str,
        ts: str,
        signature: str,
        *,
        verify: VerifyFn,
        now: datetime | None = None,
    ) -> CommunityOutcome:
        """Gate the Signal community on proven ownership + freshness + contribution.

        The checks run in a **reveal-nothing** order: an unproven caller learns
        only that its signature failed — never whether a community is configured
        or who has seeded.

        1. ``verify(pubkey, message, signature)`` over
           ``community-request:<pubkey>:<ts>`` must hold → else ``bad_signature``;
        2. ``ts`` must be a fresh ISO-8601 UTC instant within ±120 s of ``now``
           (default :func:`datetime.now` UTC) → else ``stale`` (a malformed ``ts``
           is ``stale`` too — fail-closed);
        3. an invite must be configured → else ``not_configured``;
        4. ``pubkey`` must have seeded ≥ 1 champion → else ``not_a_contributor``;
        5. otherwise the configured invite with ``reason == "ok"``.
        """
        message = f"community-request:{pubkey}:{ts}"
        if not verify(pubkey, message.encode("utf-8"), signature):
            return CommunityOutcome(invite=None, reason="bad_signature")
        if _challenge_is_stale(ts, now):
            return CommunityOutcome(invite=None, reason="stale")
        if self._signal_invite is None:
            return CommunityOutcome(invite=None, reason="not_configured")
        if not self.has_seeded(pubkey):
            return CommunityOutcome(invite=None, reason="not_a_contributor")
        return CommunityOutcome(invite=self._signal_invite, reason="ok")

    def find(self, content_hash: str) -> ArtifactRef | None:
        """Return the seeders + manifest for ``content_hash``, or ``None`` if unknown."""
        manifest = self._catalog.get(content_hash)
        if manifest is None:
            return None
        return ArtifactRef(
            content_hash=content_hash,
            seeders=list(self._seeders.get(content_hash, [])),
            manifest=manifest,
        )

    def _add_peer(self, addr: str) -> None:
        if addr not in self._peers:
            self._peers.append(addr)


# ─── the client (honest-verify HTTP; a down tracker RAISES) ──────────────


class HttpTrackerTransport:
    """A ``MeshDirectory`` client speaking HTTP to a ``TrackerRegistry`` server.

    Construct with the tracker ``base_url``. A caller may inject an ``httpx.Client``
    (tests build one on ``httpx.MockTransport``); then the caller owns its lifetime.
    When none is injected, one is created internally and closed by :meth:`close`
    (or the ``with`` block) — it is never leaked.

    Error policy is honest-verify: a transport error (tracker down, DNS, timeout)
    or a 5xx / unexpected status / unexpected body raises
    :class:`~sanctum_cli.errors.LocalError`. The sole non-exceptional "empty"
    answers are a clean ``404`` on :meth:`find` (``None``) and a legitimate
    ``{"ok": false}`` on :meth:`register` (``False``). A failure is never hidden
    behind an empty result.
    """

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        if client is None:
            self._client = httpx.Client(timeout=timeout)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Close the internally-created client; a no-op for an injected one."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpTrackerTransport:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- MeshDirectory surface ------------------------------------------

    def register(self, identity: MeshIdentity, addr: str) -> bool:
        """Advertise ``identity`` at ``addr``; return the tracker's real ack.

        ``{"ok": true}`` → ``True``, ``{"ok": false}`` → ``False`` (a genuine
        refusal). Anything else — a non-2xx, a missing/non-bool ``ok`` — raises.
        """
        data = self._json(
            self._send(
                "POST",
                "/register",
                json_body={
                    "pubkey": identity.pubkey,
                    "label": identity.label,
                    "created": identity.created,
                    "addr": addr,
                },
            )
        )
        ok = data.get("ok")
        if not isinstance(ok, bool):
            raise LocalError(
                f"mesh tracker returned no boolean 'ok' from {self._base}/register",
                fix="the tracker is misbehaving; check its version and logs",
            )
        return ok

    def peers(self) -> list[str]:
        """Return the addrs of currently-known mesh peers (raises on a down tracker)."""
        data = self._json(self._send("GET", "/peers"))
        peers = data.get("peers")
        if not isinstance(peers, list):
            raise LocalError(
                f"mesh tracker returned no 'peers' list from {self._base}/peers",
                fix="the tracker is misbehaving; check its version and logs",
            )
        return [str(p) for p in peers]

    def catalog(self) -> list[ChampionManifest]:
        """Return the champion manifests currently advertised (raises on a down tracker)."""
        data = self._json(self._send("GET", "/catalog"))
        champions = data.get("champions")
        if not isinstance(champions, list):
            raise LocalError(
                f"mesh tracker returned no 'champions' list from {self._base}/catalog",
                fix="the tracker is misbehaving; check its version and logs",
            )
        return [self._manifest_from(d, "/catalog") for d in champions]

    def announce(self, manifest: ChampionManifest, addr: str) -> None:
        """Advertise that ``addr`` seeds ``manifest`` (raises on any non-2xx)."""
        self._send("POST", "/announce", json_body={"manifest": manifest.to_dict(), "addr": addr})

    def find(self, content_hash: str) -> ArtifactRef | None:
        """Return the seeders + manifest for ``content_hash``.

        A clean ``404`` is a genuine miss → ``None``. A 2xx returns the located
        :class:`~sanctum_cli.mesh.types.ArtifactRef`; a 5xx / unexpected status
        raises — a down tracker is never mistaken for "nobody is seeding it".
        """
        resp = self._send("GET", "/find", params={"hash": content_hash})
        if resp.status_code == httpx.codes.NOT_FOUND:
            return None
        data = self._json(resp)
        try:
            return ArtifactRef(
                content_hash=str(data["content_hash"]),
                seeders=[str(s) for s in data["seeders"]],
                manifest=self._manifest_from(data["manifest"], "/find"),
            )
        except (KeyError, TypeError) as exc:
            raise LocalError(
                f"mesh tracker returned a malformed find hit from {self._base}/find",
                fix="the tracker is misbehaving; check its version and logs",
            ) from exc

    def community(self, pubkey: str, ts: str, signature: str) -> CommunityOutcome:
        """Ask the tracker for the Signal community invite, proving identity ownership.

        ``GET /community?pubkey&ts&sig``. A ``200`` carries the invite
        (``reason == "ok"``); a ``403`` carries a typed refusal
        (``bad_signature`` / ``stale`` / ``not_a_contributor``); a ``404`` means
        the tracker has no invite configured (``not_configured``). Honest-verify:
        a transport failure, a 5xx, or a malformed body raises
        :class:`~sanctum_cli.errors.LocalError` — an invite is never fabricated.
        """
        resp = self._send(
            "GET",
            "/community",
            params={"pubkey": pubkey, "ts": ts, "sig": signature},
            allow_status=_COMMUNITY_ALLOW_STATUS,
        )
        if resp.status_code == httpx.codes.NOT_FOUND:
            return CommunityOutcome(invite=None, reason="not_configured")
        if resp.status_code == httpx.codes.FORBIDDEN:
            reason = self._json(resp).get("reason")
            if reason not in _COMMUNITY_REFUSALS:
                raise LocalError(
                    f"mesh tracker returned an unknown community refusal from "
                    f"{self._base}/community",
                    fix="the tracker is misbehaving; check its version and logs",
                )
            return CommunityOutcome(invite=None, reason=reason)
        invite = self._json(resp).get("invite")
        if not isinstance(invite, str):
            raise LocalError(
                f"mesh tracker returned no 'invite' string from {self._base}/community",
                fix="the tracker is misbehaving; check its version and logs",
            )
        return CommunityOutcome(invite=invite, reason="ok")

    def _manifest_from(self, payload: Any, where: str) -> ChampionManifest:
        """Rebuild a manifest from the OPEN tracker's JSON, honest-verify style.

        The tracker is untrusted, so a missing field, a bad ``kind`` enum, or a
        wrong-typed value must surface as a clean :class:`LocalError` — never a
        raw ``KeyError``/``ValueError``/``TypeError`` leaking past the CLI's
        ``except SanctumError`` (which would crash ``status``/``pull``/the
        onboard join gate on a misbehaving tracker).
        """
        try:
            return ChampionManifest.from_dict(payload)
        except (KeyError, ValueError, TypeError) as exc:
            raise LocalError(
                f"mesh tracker returned a malformed manifest from {self._base}{where}",
                fix="the tracker is misbehaving; check its version and logs",
            ) from exc

    # -- transport internals --------------------------------------------

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, str] | None = None,
        allow_status: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """Perform one request; map a transport error and any 5xx/unexpected to LocalError.

        A ``404`` is returned as-is so :meth:`find` can read it as a miss, as is
        any status in ``allow_status`` (e.g. a ``403`` community refusal the
        caller decodes into a typed outcome); every other non-2xx (and every
        transport-level failure) is an honest raise.
        """
        url = self._base + path
        try:
            resp = self._client.request(
                method, url, json=json_body, params=params, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise LocalError(
                f"mesh tracker unreachable at {url}: {exc}",
                fix="check the tracker is running and reachable, then retry",
            ) from exc
        if resp.status_code == httpx.codes.NOT_FOUND or resp.status_code in allow_status:
            return resp
        if not resp.is_success:
            raise LocalError(
                f"mesh tracker returned HTTP {resp.status_code} from {url}",
                fix="the tracker rejected the request or is unhealthy; check its logs",
            )
        return resp

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        """Decode a JSON object body, raising LocalError on non-JSON / non-object."""
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LocalError(
                f"mesh tracker returned non-JSON from {resp.request.url}",
                fix="the tracker is misbehaving; check its version and logs",
            ) from exc
        if not isinstance(data, dict):
            raise LocalError(
                f"mesh tracker returned a non-object JSON body from {resp.request.url}",
                fix="the tracker is misbehaving; check its version and logs",
            )
        return data


if TYPE_CHECKING:

    def _conformance(x: HttpTrackerTransport) -> MeshDirectory:
        """Compile-time proof that the client satisfies the CLI's MeshDirectory.

        Never called; exists so mypy fails loudly if the two shapes ever drift.
        """
        return x


# ─── the server (thin async glue over the pure registry) ─────────────────


class TrackerHandlers:
    """aiohttp request handlers that delegate to a :class:`TrackerRegistry`.

    Each handler is intentionally thin: parse the request, call the registry,
    JSON-encode its answer. Grouped on a class (not closures) so every handler
    can be unit-tested by awaiting it with a stub request — no live socket.
    """

    def __init__(self, registry: TrackerRegistry, *, verify: VerifyFn | None = None) -> None:
        self.registry = registry
        # The Ed25519 verify seam the community gate proves ownership with —
        # injectable so the handler glue is unit-tested with a fake / real signer.
        self._verify: VerifyFn = verify if verify is not None else Ed25519Signer().verify

    async def register(self, request: web.Request) -> web.Response:
        """``POST /register`` → ``{"ok": bool}`` (the registry's ack)."""
        body = await request.json()
        ok = self.registry.register(
            str(body["pubkey"]),
            str(body["label"]),
            str(body["created"]),
            str(body["addr"]),
        )
        return web.json_response({"ok": ok})

    async def peers(self, _request: web.Request) -> web.Response:
        """``GET /peers`` → ``{"peers": [...]}``."""
        return web.json_response({"peers": self.registry.list_peers()})

    async def catalog(self, _request: web.Request) -> web.Response:
        """``GET /catalog`` → ``{"champions": [manifest_dict, ...]}``."""
        return web.json_response(
            {"champions": [m.to_dict() for m in self.registry.list_catalog()]}
        )

    async def announce(self, request: web.Request) -> web.Response:
        """``POST /announce`` → store the manifest + seeder; ``{"ok": true}``."""
        body = await request.json()
        manifest = ChampionManifest.from_dict(body["manifest"])
        self.registry.announce(manifest, str(body["addr"]))
        return web.json_response({"ok": True})

    async def find(self, request: web.Request) -> web.Response:
        """``GET /find?hash=…`` → the hit body, or ``404`` on a genuine miss."""
        content_hash = request.query.get("hash", "")
        ref = self.registry.find(content_hash)
        if ref is None:
            return web.json_response(
                {"error": "not found", "hash": content_hash}, status=404
            )
        return web.json_response(
            {
                "content_hash": ref.content_hash,
                "seeders": ref.seeders,
                "manifest": ref.manifest.to_dict(),
            }
        )

    async def community(self, request: web.Request) -> web.Response:
        """``GET /community?pubkey&ts&sig`` → 200 invite / 403 refusal / 404 unconfigured.

        Maps the registry's :class:`CommunityOutcome`: ``ok`` →
        ``200 {"invite": …}``; ``bad_signature`` / ``stale`` /
        ``not_a_contributor`` → ``403 {"reason": …}``; ``not_configured`` →
        ``404 {"reason": "not_configured"}``.
        """
        outcome = self.registry.community(
            request.query.get("pubkey", ""),
            request.query.get("ts", ""),
            request.query.get("sig", ""),
            verify=self._verify,
        )
        if outcome.reason == "ok":
            return web.json_response({"invite": outcome.invite})
        if outcome.reason == "not_configured":
            return web.json_response({"reason": "not_configured"}, status=404)
        return web.json_response({"reason": outcome.reason}, status=403)


def build_tracker_app(registry: TrackerRegistry | None = None) -> web.Application:
    """Wire the discovery + community routes to a fresh (or injected) :class:`TrackerRegistry`.

    Returns an ``aiohttp.web.Application`` ready for :func:`serve` (or for a test
    that inspects its routes). The routes are ``POST /register``, ``GET /peers``,
    ``GET /catalog``, ``POST /announce``, ``GET /find``, and ``GET /community``.
    """
    handlers = TrackerHandlers(registry if registry is not None else TrackerRegistry())
    app = web.Application()
    app.router.add_post("/register", handlers.register)
    app.router.add_get("/peers", handlers.peers)
    app.router.add_get("/catalog", handlers.catalog)
    app.router.add_post("/announce", handlers.announce)
    app.router.add_get("/find", handlers.find)
    app.router.add_get("/community", handlers.community)
    return app


def serve(
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    registry: TrackerRegistry | None = None,
) -> None:  # pragma: no cover - blocking live server, exercised by the e2e drill
    """Run the tracker app (blocking) on ``host:port`` — the real loopback server."""
    web.run_app(build_tracker_app(registry), host=host, port=port)
