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

import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import httpx

from sanctum_cli.errors import LocalError
from sanctum_cli.mesh import artifact
from sanctum_cli.mesh.adapters import Ed25519Signer
from sanctum_cli.mesh.types import (
    ArtifactRef,
    ChampionManifest,
    MeshAnalyticsSummary,
    MeshIdentity,
    NodeMacroMetrics,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType, TracebackType

    from aiohttp import web

    # Import-for-typing only: a runtime import would be circular (commands.mesh
    # imports the mesh package). Structural typing makes HttpTrackerTransport a
    # MeshDirectory without any runtime coupling — see ``_conformance`` below.
    from sanctum_cli.commands.mesh import MeshDirectory

    # The Ed25519 verify seam ``(pubkey, message, signature) -> ok`` — the mesh's
    # single crypto-verify Protocol, reused verbatim so announce-credit and the
    # community gate prove ownership through one shape (a PQ signer drops in behind
    # it, and :func:`artifact.verify_signature` accepts it directly). Injectable so
    # the whole gate is unit-testable with a fake / real signer.
    from sanctum_cli.mesh.artifact import SigningIdentity, VerifyFn

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
_ENV_TRACKER_TOKEN = "SANCTUM_MESH_TRACKER_TOKEN"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_MAX_SEEN_NONCES = 10_000

# The signed challenge is fresh within ±120 s of the tracker's clock —
# a small window that bounds replay of a captured request (combined with a
# bounded seen-signature replay cache on the handlers).
_COMMUNITY_SKEW_SECONDS = 120.0
# The typed 403 refusals the client will accept back on the wire; anything else
# in a 403 body is a misbehaving tracker (honest-verify raise).
_COMMUNITY_REFUSALS = frozenset({"bad_signature", "stale", "not_a_contributor"})
# 403 must pass through _send (a legitimate refusal body), unlike a 5xx (raise).
_COMMUNITY_ALLOW_STATUS = frozenset({int(httpx.codes.FORBIDDEN)})


def _register_signing_message(
    pubkey: str, label: str, created: str, addr: str, ts: str
) -> bytes:
    """Canonical bytes signed by a node for ``POST /register``."""
    return f"sanctum-mesh-register\n{pubkey}\n{label}\n{created}\n{addr}\n{ts}".encode()


def _announce_signing_message(pubkey: str, content_hash: str, addr: str, ts: str) -> bytes:
    """Canonical bytes signed by a node for ``POST /announce``."""
    return f"sanctum-mesh-announce\n{pubkey}\n{content_hash}\n{addr}\n{ts}".encode()


def _normalize_token(token: str | None) -> str | None:
    if token is None:
        return None
    stripped = token.strip()
    return stripped or None


def _bearer_matches(header_value: str | None, expected: str) -> bool:
    """Constant-time comparison of an HTTP ``Authorization: Bearer <token>`` header."""
    if not header_value:
        return False
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    got = parts[1].strip()
    if not got:
        return False
    return hmac.compare_digest(
        hashlib.sha256(got.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


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

    def __init__(
        self, signal_invite: str | None = None, *, verify: VerifyFn | None = None
    ) -> None:
        self._peers: list[str] = []
        self._identities: dict[str, MeshIdentity] = {}
        self._catalog: dict[str, ChampionManifest] = {}
        self._seeders: dict[str, list[str]] = {}
        # Every pubkey that has announced ≥ 1 champion it SELF-SIGNED — the
        # contribute-to-join set the community gate reads. A manifest whose
        # signature does not verify under its own producer key is still stored +
        # findable (the tracker stays a dumb discovery pointer) but never earns
        # the contributor bit.
        self._seeded_pubkeys: set[str] = set()
        # The single Ed25519 verify seam BOTH announce-credit and the community
        # gate prove ownership with — injectable so the whole gate is unit-tested
        # with a fake / real signer, and so a PQ signer drops in behind one shape.
        self._verify: VerifyFn = verify if verify is not None else Ed25519Signer().verify
        # Anonymized node macro metrics for privacy-preserving mesh analytics
        self._node_metrics: dict[str, NodeMacroMetrics] = {}
        # ``signal_invite`` is operator config (the Signal group link). ``None`` OR
        # an empty/whitespace-only string means no community is set up on this mesh
        # — normalized to ``None`` here so ``community`` returns ``not_configured``
        # and the client never yields an empty invite.
        self._signal_invite = signal_invite if (signal_invite and signal_invite.strip()) else None

    def register(self, pubkey: str, label: str, created: str, addr: str) -> bool:
        """Record ``(pubkey, label, created)`` at ``addr`` and ack.

        Idempotent for the owning key: re-registering an addr with the same
        ``pubkey`` refreshes its identity and never duplicates it in ``peers``.
        If ``addr`` is already bound to a different ``pubkey``, returns ``False``
        (fail-closed against address hijacking).
        """
        existing = self._identities.get(addr)
        if existing is not None and existing.pubkey != pubkey:
            return False
        self._identities[addr] = MeshIdentity(pubkey=pubkey, label=label, created=created)
        self._add_peer(addr)
        return True

    def identity_for_addr(self, addr: str) -> MeshIdentity | None:
        """Return the registered :class:`MeshIdentity` for ``addr``, if any."""
        return self._identities.get(addr)

    def catalog_entry(self, content_hash: str) -> ChampionManifest | None:
        """Return the catalogued :class:`ChampionManifest` for ``content_hash``, if any."""
        return self._catalog.get(content_hash)

    def verify_signature(self, pubkey: str, message: bytes, signature: str) -> bool:
        """Verify ``signature`` over ``message`` using the registry's verify seam."""
        return self._verify(pubkey, message, signature)

    def verify_manifest_signature(self, manifest: ChampionManifest) -> bool:
        """Verify ``manifest``'s self-signature using the registry's verify seam."""
        return artifact.verify_signature(manifest, self._verify)

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
        # Credit the producer as a contributor ONLY when the manifest carries a
        # valid self-signature over its own producer key — a cryptographic proof of
        # authorship. The catalog + seeder storage above is UNCHANGED: the tracker
        # stays a dumb discovery pointer, so an unsigned/forged manifest is still
        # stored + findable; it simply does not earn the contributor bit that
        # unlocks the community gate.
        if artifact.verify_signature(manifest, self._verify):
            self._seeded_pubkeys.add(manifest.producer_pubkey)

    def has_seeded(self, pubkey: str) -> bool:
        """Return whether ``pubkey`` has announced at least one SELF-SIGNED champion.

        A pubkey becomes a contributor the instant :meth:`announce` records a
        manifest it produced AND validly self-signed — the mesh's
        contribute-to-join gate (an unsigned/forged announce never counts).
        """
        return pubkey in self._seeded_pubkeys

    def community(
        self,
        pubkey: str,
        ts: str,
        signature: str,
        *,
        now: datetime | None = None,
    ) -> CommunityOutcome:
        """Gate the Signal community on proven ownership + freshness + contribution.

        The checks run in a **reveal-nothing** order: an UNPROVEN caller learns
        only that its signature failed — never whether a community is configured or
        who has seeded. A PROVEN caller who is not a contributor is told exactly
        that and STILL does not learn whether a community is configured (the
        contributor check precedes the config-existence check).

        1. ``self._verify(pubkey, message, signature)`` over
           ``community-request:<pubkey>:<ts>`` must hold → else ``bad_signature``;
        2. ``ts`` must be a fresh ISO-8601 UTC instant within ±120 s of ``now``
           (default :func:`datetime.now` UTC) → else ``stale`` (a malformed ``ts``
           is ``stale`` too — fail-closed);
        3. ``pubkey`` must have self-signed ≥ 1 announced champion → else
           ``not_a_contributor`` (checked BEFORE config-existence so a proven
           non-contributor cannot probe whether a community even exists);
        4. an invite must be configured → else ``not_configured``;
        5. otherwise the configured invite with ``reason == "ok"``.
        """
        message = f"community-request:{pubkey}:{ts}"
        if not self._verify(pubkey, message.encode("utf-8"), signature):
            return CommunityOutcome(invite=None, reason="bad_signature")
        if _challenge_is_stale(ts, now):
            return CommunityOutcome(invite=None, reason="stale")
        if not self.has_seeded(pubkey):
            return CommunityOutcome(invite=None, reason="not_a_contributor")
        if self._signal_invite is None:
            return CommunityOutcome(invite=None, reason="not_configured")
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

    def record_pulse(self, metrics: NodeMacroMetrics) -> bool:
        """Record an anonymized node metrics pulse."""
        self._node_metrics[metrics.node_id] = metrics
        return True

    def analytics_summary(self) -> MeshAnalyticsSummary:
        """Produce an aggregated summary of active mesh nodes."""
        nodes = list(self._node_metrics.values())
        total = len(nodes)
        if total == 0:
            return MeshAnalyticsSummary(
                total_nodes=0,
                countries={},
                chips={},
                memory_tiers_gb={},
                mean_offline_ratio=0.0,
                max_eval_baseline=0.0,
                total_champions=len(self._catalog),
            )
        countries: dict[str, int] = {}
        chips: dict[str, int] = {}
        mem_tiers: dict[str, int] = {}
        offline_sum = 0.0
        max_eval = 0.0
        for m in nodes:
            countries[m.country] = countries.get(m.country, 0) + 1
            chips[m.chip] = chips.get(m.chip, 0) + 1
            tier = f"{m.memory_gb}GB"
            mem_tiers[tier] = mem_tiers.get(tier, 0) + 1
            offline_sum += m.offline_ratio
            if m.eval_baseline > max_eval:
                max_eval = m.eval_baseline
        return MeshAnalyticsSummary(
            total_nodes=total,
            countries=countries,
            chips=chips,
            memory_tiers_gb=mem_tiers,
            mean_offline_ratio=round(offline_sum / total, 3),
            max_eval_baseline=round(max_eval, 4),
            total_champions=len(self._catalog),
        )


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
        identity: SigningIdentity | None = None,
        identity_provider: Callable[[], SigningIdentity | None] | None = None,
        auth_token: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._identity = identity
        self._identity_provider = identity_provider
        self._auth_token = _normalize_token(auth_token)
        if client is None:
            self._client = httpx.Client(timeout=timeout)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    def _signing_identity(self, *, expected_pubkey: str | None = None) -> SigningIdentity:
        candidate = (
            self._identity
            if self._identity is not None
            else (self._identity_provider() if self._identity_provider is not None else None)
        )
        if candidate is None:
            raise LocalError(
                "mesh identity required for authenticated tracker write",
                fix="run 'sanctum mesh join' to mint a mesh identity",
            )
        if expected_pubkey is not None and candidate.pubkey != expected_pubkey:
            raise LocalError(
                "mesh signing identity does not match the registered pubkey",
                fix="re-run 'sanctum mesh join' with your node's identity",
            )
        return candidate

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

        Signs ``(pubkey, label, created, addr, ts)`` with this node's Ed25519
        identity so the tracker proves key ownership on every registration.
        ``{"ok": true}`` → ``True``, ``{"ok": false}`` → ``False`` (a genuine
        refusal). Anything else — a non-2xx, a missing/non-bool ``ok`` — raises.
        """
        signer = self._signing_identity(expected_pubkey=identity.pubkey)
        ts = datetime.now(UTC).isoformat()
        sig = signer.sign(
            _register_signing_message(
                identity.pubkey, identity.label, identity.created, addr, ts
            )
        )
        data = self._json(
            self._send(
                "POST",
                "/register",
                json_body={
                    "pubkey": identity.pubkey,
                    "label": identity.label,
                    "created": identity.created,
                    "addr": addr,
                    "ts": ts,
                    "signature": sig,
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
        signer = self._signing_identity()
        ts = datetime.now(UTC).isoformat()
        sig = signer.sign(
            _announce_signing_message(signer.pubkey, manifest.content_hash, addr, ts)
        )
        self._send(
            "POST",
            "/announce",
            json_body={
                "manifest": manifest.to_dict(),
                "addr": addr,
                "pubkey": signer.pubkey,
                "ts": ts,
                "signature": sig,
            },
        )

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

        ``POST /community`` with a ``{pubkey, ts, signature}`` body — the signed
        challenge is a CREDENTIAL and travels in the request body, never a URL
        query (access logs / referrers must not capture it). A ``200`` carries the
        invite (``reason == "ok"``); a ``403`` carries a typed refusal
        (``bad_signature`` / ``stale`` / ``not_a_contributor``); a ``404`` means
        the tracker has no invite configured (``not_configured``). Honest-verify:
        a transport failure, a 5xx, or a malformed body raises
        :class:`~sanctum_cli.errors.LocalError` — an invite is never fabricated.
        """
        resp = self._send(
            "POST",
            "/community",
            json_body={"pubkey": pubkey, "ts": ts, "signature": signature},
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

    def pulse(self, metrics: NodeMacroMetrics) -> bool:
        """Post anonymized node metrics pulse to the tracker. Returns True on ack."""
        resp = self._send("POST", "/pulse", json_body=metrics.to_dict())
        data = self._json(resp)
        return bool(data.get("ok", False))

    def analytics(self) -> MeshAnalyticsSummary:
        """Fetch aggregated swarm macro analytics from the tracker."""
        resp = self._send("GET", "/analytics")
        data = self._json(resp)
        try:
            return MeshAnalyticsSummary.from_dict(data)
        except (KeyError, TypeError) as exc:
            raise LocalError(
                f"mesh tracker returned malformed analytics from {self._base}/analytics",
                fix="the tracker is misbehaving; check its version and logs",
            ) from exc

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
        headers: dict[str, str] | None = None
        if self._auth_token is not None:
            headers = {"Authorization": f"Bearer {self._auth_token}"}
        try:
            resp = self._client.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=headers,
                timeout=self._timeout,
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


def _aiohttp_web() -> ModuleType:
    """Import aiohttp only when serving — the HTTP *client* must not need it.

    ``sanctum endocrine tick`` (and every other CLI command) imports
    ``commands.mesh`` → this module. A missing aiohttp in the uv-tool env
    used to brick the gland tick and page Force Flow GLAND_DOWN forever
    (2026-08-16). TrackerRegistry + HttpTrackerTransport stay importable
    without the server extra.
    """
    try:
        from aiohttp import web
    except ImportError as exc:  # pragma: no cover - env gap, not a logic path
        raise LocalError(
            "aiohttp is required to serve the mesh tracker (declared in "
            "pyproject; reinstall the sanctum-cli uv tool)"
        ) from exc
    return web


def _json_response(payload: dict[str, Any], *, status: int = 200) -> web.Response:
    """Typed wrapper over the lazily-imported ``web.json_response``.

    `_aiohttp_web` can only be annotated as ``ModuleType``, whose attributes
    are ``Any``, so every handler that called ``web.json_response`` directly
    returned ``Any`` from a function declared to return ``web.Response`` —
    nineteen mypy errors under this package's ``strict = true``, all from one
    unannotated helper. Centralising the cast here keeps the handlers honestly
    typed and puts the single unavoidable narrowing in one reviewable place.
    """
    return cast("web.Response", _aiohttp_web().json_response(payload, status=status))


class TrackerHandlers:
    """aiohttp request handlers that delegate to a :class:`TrackerRegistry`.

    Each handler is intentionally thin: authenticate the request, parse it,
    call the registry, JSON-encode its answer. Grouped on a class (not closures)
    so every handler can be unit-tested by awaiting it with a stub request — no
    live socket.
    """

    def __init__(self, registry: TrackerRegistry, *, auth_token: str | None = None) -> None:
        self.registry = registry
        self._auth_token = _normalize_token(auth_token)
        self._seen_nonces: dict[tuple[str, str, str], datetime] = {}

    def _require_bearer(self, request: web.Request) -> web.Response | None:
        if self._auth_token is None:
            return None
        headers = getattr(request, "headers", None)
        auth_header: str | None = None
        if headers is not None:
            auth_header = next(
                (v for k, v in headers.items() if k.lower() == "authorization"),
                None,
            )
        if not _bearer_matches(auth_header, self._auth_token):
            return _json_response({"ok": False, "reason": "unauthorized"}, status=401)
        return None

    def _prune_seen_nonces(self, now: datetime) -> None:
        cutoff = _COMMUNITY_SKEW_SECONDS * 2.0
        expired = [
            key
            for key, seen_at in self._seen_nonces.items()
            if abs((now - seen_at).total_seconds()) > cutoff
        ]
        for key in expired:
            del self._seen_nonces[key]
        while len(self._seen_nonces) >= _MAX_SEEN_NONCES:
            oldest = next(iter(self._seen_nonces))
            del self._seen_nonces[oldest]

    def _is_replayed(self, pubkey: str, ts: str, signature: str, now: datetime) -> bool:
        self._prune_seen_nonces(now)
        return (pubkey, ts, signature) in self._seen_nonces

    def _record_nonce(self, pubkey: str, ts: str, signature: str, now: datetime) -> None:
        self._prune_seen_nonces(now)
        self._seen_nonces[(pubkey, ts, signature)] = now

    async def register(self, request: web.Request) -> web.Response:
        """``POST /register`` → signed identity registration; ``{"ok": bool}``."""
        if (unauth := self._require_bearer(request)) is not None:
            return unauth
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return _json_response({"ok": False, "reason": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return _json_response({"ok": False, "reason": "invalid_json"}, status=400)

        pubkey = body.get("pubkey")
        label = body.get("label")
        created = body.get("created")
        addr = body.get("addr")
        ts = body.get("ts")
        signature = body.get("signature")
        if (
            not isinstance(pubkey, str)
            or not pubkey.strip()
            or not isinstance(label, str)
            or not isinstance(created, str)
            or not isinstance(addr, str)
            or not addr.strip()
            or not isinstance(ts, str)
            or not ts.strip()
            or not isinstance(signature, str)
            or not signature.strip()
        ):
            return _json_response({"ok": False, "reason": "unauthorized"}, status=401)

        now = datetime.now(UTC)
        if _challenge_is_stale(ts, now):
            return _json_response({"ok": False, "reason": "stale"}, status=403)
        if self._is_replayed(pubkey, ts, signature, now):
            return _json_response({"ok": False, "reason": "replayed"}, status=403)
        msg = _register_signing_message(pubkey, label, created, addr, ts)
        if not self.registry.verify_signature(pubkey, msg, signature):
            return _json_response({"ok": False, "reason": "bad_signature"}, status=403)
        self._record_nonce(pubkey, ts, signature, now)

        ok = self.registry.register(pubkey, label, created, addr)
        if not ok:
            return _json_response({"ok": False, "reason": "addr_owned"}, status=403)
        return _json_response({"ok": True})

    async def peers(self, _request: web.Request) -> web.Response:
        """``GET /peers`` → ``{"peers": [...]}``."""
        return _json_response({"peers": self.registry.list_peers()})

    async def catalog(self, _request: web.Request) -> web.Response:
        """``GET /catalog`` → ``{"champions": [manifest_dict, ...]}``."""
        return _json_response(
            {"champions": [m.to_dict() for m in self.registry.list_catalog()]}
        )

    async def announce(self, request: web.Request) -> web.Response:
        """``POST /announce`` → signed announce + manifest verify + conflict check."""
        if (unauth := self._require_bearer(request)) is not None:
            return unauth
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return _json_response({"ok": False, "reason": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return _json_response({"ok": False, "reason": "invalid_json"}, status=400)

        pubkey = body.get("pubkey")
        addr = body.get("addr")
        ts = body.get("ts")
        signature = body.get("signature")
        manifest_raw = body.get("manifest")
        if (
            not isinstance(pubkey, str)
            or not pubkey.strip()
            or not isinstance(addr, str)
            or not addr.strip()
            or not isinstance(ts, str)
            or not ts.strip()
            or not isinstance(signature, str)
            or not signature.strip()
        ):
            return _json_response({"ok": False, "reason": "unauthorized"}, status=401)
        if not isinstance(manifest_raw, Mapping):
            return _json_response({"ok": False, "reason": "invalid_manifest"}, status=400)
        try:
            manifest = ChampionManifest.from_dict(manifest_raw)
        except (KeyError, ValueError, TypeError):
            return _json_response({"ok": False, "reason": "invalid_manifest"}, status=400)

        now = datetime.now(UTC)
        if _challenge_is_stale(ts, now):
            return _json_response({"ok": False, "reason": "stale"}, status=403)
        if self._is_replayed(pubkey, ts, signature, now):
            return _json_response({"ok": False, "reason": "replayed"}, status=403)
        msg = _announce_signing_message(pubkey, manifest.content_hash, addr, ts)
        if not self.registry.verify_signature(pubkey, msg, signature):
            return _json_response({"ok": False, "reason": "bad_signature"}, status=403)
        self._record_nonce(pubkey, ts, signature, now)

        owner = self.registry.identity_for_addr(addr)
        if owner is not None and owner.pubkey != pubkey:
            return _json_response({"ok": False, "reason": "addr_owned"}, status=403)
        if not self.registry.verify_manifest_signature(manifest):
            return _json_response({"ok": False, "reason": "bad_manifest_signature"}, status=403)
        existing_manifest = self.registry.catalog_entry(manifest.content_hash)
        if existing_manifest is not None and existing_manifest != manifest:
            return _json_response({"ok": False, "reason": "catalog_conflict"}, status=409)

        self.registry.announce(manifest, addr)
        return _json_response({"ok": True})

    async def find(self, request: web.Request) -> web.Response:
        """``GET /find?hash=…`` → the hit body, or ``404`` on a genuine miss."""
        content_hash = request.query.get("hash", "")
        ref = self.registry.find(content_hash)
        if ref is None:
            return _json_response(
                {"error": "not found", "hash": content_hash}, status=404
            )
        return _json_response(
            {
                "content_hash": ref.content_hash,
                "seeders": ref.seeders,
                "manifest": ref.manifest.to_dict(),
            }
        )

    async def community(self, request: web.Request) -> web.Response:
        """``POST /community`` {pubkey, ts, signature} → 200 invite / 403 refusal / 404.

        The signed challenge is a credential, so it arrives in the POST BODY, never
        a URL query (access logs / referrers must not capture it). Maps the
        registry's :class:`CommunityOutcome`: ``ok`` → ``200 {"invite": …}``;
        ``bad_signature`` / ``stale`` / ``not_a_contributor`` →
        ``403 {"reason": …}``; ``not_configured`` →
        ``404 {"reason": "not_configured"}``.
        """
        body = await request.json()
        outcome = self.registry.community(
            str(body.get("pubkey", "")),
            str(body.get("ts", "")),
            str(body.get("signature", "")),
        )
        if outcome.reason == "ok":
            return _json_response({"invite": outcome.invite})
        if outcome.reason == "not_configured":
            return _json_response({"reason": "not_configured"}, status=404)
        return _json_response({"reason": outcome.reason}, status=403)

    async def pulse(self, request: web.Request) -> web.Response:
        """``POST /pulse`` → record anonymized node pulse; ``{"ok": true}``."""
        if (unauth := self._require_bearer(request)) is not None:
            return unauth
        try:
            body = await request.json()
            metrics = NodeMacroMetrics.from_dict(body)
        except (ValueError, TypeError, KeyError):
            return _json_response({"ok": False, "reason": "invalid_pulse"}, status=400)
        ok = self.registry.record_pulse(metrics)
        return _json_response({"ok": ok})

    async def analytics(self, _request: web.Request) -> web.Response:
        """``GET /analytics`` → aggregated :class:`MeshAnalyticsSummary`."""
        summary = self.registry.analytics_summary()
        return _json_response(summary.to_dict())


def build_tracker_app(
    registry: TrackerRegistry | None = None,
    *,
    auth_token: str | None = None,
) -> web.Application:
    """Wire the discovery + community routes to a fresh (or injected) :class:`TrackerRegistry`.

    Returns an ``aiohttp.web.Application`` ready for :func:`serve` (or for a test
    that inspects its routes). The routes are ``POST /register``, ``GET /peers``,
    ``GET /catalog``, ``POST /announce``, ``GET /find``, ``POST /community``,
    ``POST /pulse``, and ``GET /analytics``.
    """
    web_mod = _aiohttp_web()
    resolved_token = _normalize_token(
        auth_token if auth_token is not None else os.environ.get(_ENV_TRACKER_TOKEN)
    )
    handlers = TrackerHandlers(
        registry if registry is not None else TrackerRegistry(),
        auth_token=resolved_token,
    )
    app = cast("web.Application", web_mod.Application())
    app.router.add_post("/register", handlers.register)
    app.router.add_get("/peers", handlers.peers)
    app.router.add_get("/catalog", handlers.catalog)
    app.router.add_post("/announce", handlers.announce)
    app.router.add_get("/find", handlers.find)
    app.router.add_post("/community", handlers.community)
    app.router.add_post("/pulse", handlers.pulse)
    app.router.add_get("/analytics", handlers.analytics)
    return app


def serve(
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    registry: TrackerRegistry | None = None,
    *,
    auth_token: str | None = None,
    allow_unauthenticated_nonlocal: bool = False,
) -> None:  # pragma: no cover - blocking live server, exercised by the e2e drill
    """Run the tracker app (blocking) on ``host:port``.

    Fails closed when binding a non-loopback ``host`` without a configured
    Bearer ``auth_token`` (unless ``allow_unauthenticated_nonlocal=True``).
    """
    resolved_token = _normalize_token(
        auth_token if auth_token is not None else os.environ.get(_ENV_TRACKER_TOKEN)
    )
    if (
        host not in _LOOPBACK_HOSTS
        and resolved_token is None
        and not allow_unauthenticated_nonlocal
    ):
        raise LocalError(
            f"refusing to serve mesh tracker on non-loopback host {host!r} without auth_token",
            fix=f"set {_ENV_TRACKER_TOKEN} or pass auth_token, or bind 127.0.0.1",
        )
    web_mod = _aiohttp_web()
    web_mod.run_app(
        build_tracker_app(registry, auth_token=resolved_token),
        host=host,
        port=port,
    )
