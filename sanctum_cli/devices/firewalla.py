"""Firewalla provider — the firewall brand on the DeviceProvider contract.

A Firewalla box (Gold / Gold Pro / Purple / Red) is driven through TWO
transports, hidden behind the uniform :class:`DeviceProvider` surface:

* the **bridge HTTP** API (Bearer-token authed, default
  ``http://127.0.0.1:1984``) for reads, ``/info``, and policy state — the same
  surface the existing :mod:`sanctum_cli.commands.screen_time` engine reads
  through; and
* the **durable SSH key** (``firewalla.ssh_key`` from instance.yaml, the
  key-auth path that is immune to the box's expiring app password) for the few
  box-level operations the bridge does not expose.

The split is deliberate: every *read* and every *policy* op goes over the
bridge (legible, token-authed, fail-soft to ``None``); the SSH key is resolved
and carried so a future box-level op (a maintenance command the bridge does not
proxy) has a durable transport — but no live SSH is fired in the unit-tested
paths, and the overnight build never mutates live gear.

``connect`` resolves the bearer token (env → on-disk
``~/.sanctum/secrets/firewalla-bridge-token``) and the SSH key path, then probes
``/info`` to refine ``brand`` from ``firewalla`` to ``firewalla-<model>``. Reads
are best-effort: a path the bridge has no body for returns ``None`` (a normal
outcome, not an error). Mutating ops (``set``, the policy ``rollback`` restore)
go through the bridge ``POST`` seam and return an :class:`OpResult`; they are
composed behind :func:`sanctum_cli.devices.rails.guarded_apply` at the intent
layer (dry-run / guarded_apply rails) and are NEVER auto-fired.

Both transport seams (``_fetch_bridge_json`` / ``_post_bridge_json`` /
``_ssh_port_open`` / the token + key resolvers) are module-level so tests can
monkeypatch them and never open a socket. A live read-only smoke is env-gated
behind ``SANCTUM_LIVE_FIREWALLA=1`` (see ``tests/devices/test_firewalla.py``).
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from sanctum_cli import config
from sanctum_cli.devices import creds as creds_resolver
from sanctum_cli.devices import registry
from sanctum_cli.devices.base import (
    Capability,
    CapabilityBinding,
    CapabilityMap,
    CapabilityOp,
    Creds,
    DeviceError,
    NetContext,
    OpResult,
    Snapshot,
    build_capability_map,
)

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

# Bridge transport config — mirrors the existing screen_time engine so the two
# read the SAME box through the SAME token (single source of truth on disk).
_BRIDGE_URL_ENV = "FIREWALLA_BRIDGE_URL"
_BRIDGE_TOKEN_ENV = "FIREWALLA_BRIDGE_TOKEN"
_BRIDGE_TOKEN_FILE = Path.home() / ".sanctum/secrets/firewalla-bridge-token"
_DEFAULT_BRIDGE_URL = "http://127.0.0.1:1984"

# The headless device-credential tier the bridge token falls back to when env +
# on-disk secret both miss: macOS Keychain (service ``firewalla-app``) → SOPS
# ``devices.firewalla_app.password`` (age-key), NEVER 1Password/op. The account is
# resolved discovery-first from instance.yaml (``devices.firewalla.keychain.account``)
# with an EMPTY default — so an unconfigured box (and every test) misses cleanly to
# None, exactly as before, while a haus that seeds the entry gets the headless tier.
_FIREWALLA_KEYCHAIN_SERVICE = "firewalla-app"

# The durable key-auth host:port for the SSH fallback transport. The key path
# itself is resolved from instance.yaml (``firewalla.ssh_key``) — never
# hardcoded — so a contributor points it at their own box.
_SSH_HOST = "firewalla.local"
_SSH_PORT = 22
_SSH_PROBE_TIMEOUT_S = 1.0

# The policy subtree we snapshot before a mutating intent. A policy-state
# snapshot is the restorable baseline ``rollback`` reconciles the live box against.
#
# HONEST-VERIFY: there is NO ``POST /policies/restore`` in the Firewalla bridge
# contract — the shipping surface GETs /info, /policies, /host/<mac>, /hosts and
# mutates per-policy. So ``rollback`` does NOT hit a phantom bulk-restore route; it
# reconciles the captured baseline against the live ``/policies`` state using the
# routes that DO exist: ``DELETE /policy/:pid`` (for a policy added since the
# snapshot) and the ``POST /raw`` ``policy:create`` escape hatch (for a policy
# removed since the snapshot). It is fail-closed — an absent baseline, an unreadable
# live state, or any primitive that reports failure makes the whole rollback
# ``ok=False`` so the rails surface a manual-recovery instruction, never a false
# success.
_POLICIES_PATH = "/policies"
_INFO_PATH = "/info"


def _policies_by_pid(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Index a ``GET /policies`` reply by its string pid.

    The bridge answers ``{"policies": [...], "count": N}`` where each policy carries
    a ``pid`` (a string on the wire, e.g. ``"7"``). Returns a ``{pid: policy}`` map so
    :meth:`FirewallaProvider.rollback` can diff the captured baseline against the live
    state by id. A non-dict payload, a missing/!list ``policies`` field, or a policy
    with no ``pid`` is skipped (it cannot be addressed by ``DELETE /policy/:pid`` nor
    re-created deterministically) — keeping the diff honest about what it can restore.
    """
    if not payload:
        return {}
    policies = payload.get("policies")
    if not isinstance(policies, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for pol in policies:
        if isinstance(pol, dict) and pol.get("pid") is not None:
            indexed[str(pol["pid"])] = pol
    return indexed

# HTTP timeout for bridge calls (seconds). Matches the screen_time engine.
_HTTP_TIMEOUT_S = 15

# RFC-3986 path-character safe set for boundary encoding. Keeps every byte that
# is *already* legal, un-encoded, in a URL path literal — the '/' separators,
# ':' (so a ``/host/AA:BB:CC:..`` MAC read rides byte-identical to the prior
# wire), '@', the sub-delims ``!$&'()*+,;=``, and the unreserved punctuation
# ``-._~``. Only a byte that is NOT in this set is percent-encoded — crucially a
# literal '%' (NOT listed) becomes '%25', so an id literally containing ``%41``
# encodes once to ``%2541`` (the id's own bytes) instead of being preserved by
# httpx and mis-decoded server-side to the letter 'A'. Spaces and non-ASCII are
# likewise encoded. This is the prior code's literal-'%' fix WITHOUT the
# colon-encoding regression the bare ``safe="/"`` introduced.
_PATH_SAFE = "/:@!$&'()*+,;=~-._"


def _bridge_url() -> str:
    """Resolve the bridge base URL (env override → loopback default)."""
    return os.environ.get(_BRIDGE_URL_ENV, _DEFAULT_BRIDGE_URL)


def _encode_path(path: str) -> str:
    """Percent-encode a caller-supplied bridge path EXACTLY once for the wire.

    Own the escaping at the boundary (CLAUDE.md "Own the escaping at the
    boundary"): a target / policy id interpolated into the URL may carry a literal
    ``%``, a space, or a non-ASCII char. ``httpx`` *preserves* an existing
    ``%``-sequence, so without this an id literally containing ``%41`` would ride
    to the box as ``%41`` (decoding server-side to the letter ``A``) and silently
    address the WRONG policy. ``quote(path, safe=_PATH_SAFE)`` turns that literal
    ``%`` into ``%25`` (``%41`` → ``%2541``, the id's own bytes) — one layer of
    encoding the provider owns, so httpx has nothing left to encode and cannot
    double-encode it to ``%2525``.

    The safe set is the full RFC-3986 path-character class (``_PATH_SAFE``), NOT
    the bare ``"/"`` an earlier cut used. That earlier set encoded ``':'`` to
    ``%3A``, which silently changed the shipped screen-time read path
    ``/host/AA:BB:CC:..`` (a colon is a legal, literal path char) and rests on an
    unverifiable cross-layer assumption that the bridge route-matches ``%3A`` the
    same as ``':'``. Keeping ``':'`` and the other pchars literal sends every
    previously-working path byte-identical to the pre-refactor wire while still
    closing the literal-``%`` footgun.
    """
    return quote(path, safe=_PATH_SAFE)


def _bridge_transport() -> httpx.BaseTransport | None:
    """The httpx transport the bridge client is built on (``None`` = default).

    A seam so tests can inject an ``httpx.MockTransport`` and drive the REAL httpx
    URL-construction/encoding without opening a socket — proving the boundary
    encoding against the exact bytes that would hit the wire. In production it
    returns ``None`` so ``httpx.Client`` uses its own default (loopback) transport.
    """
    return None


def _read_bridge_token() -> str | None:
    """Resolve the bridge bearer token: env → on-disk secret file.

    Returns ``None`` when no token is available anywhere (so a caller fail-softs
    instead of sending an unauthenticated probe). The on-disk path is the
    documented ``~/.sanctum/secrets/firewalla-bridge-token``; its contents are
    stripped of trailing whitespace/newline.
    """
    token = os.environ.get(_BRIDGE_TOKEN_ENV, "").strip()
    if token:
        return token
    try:
        file_token = _BRIDGE_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        file_token = ""
    if file_token:
        return file_token
    # Last tier: the shared headless resolver (Keychain firewalla-app → SOPS
    # devices.firewalla_app.password, NEVER op/1P). Best-effort — a miss/locked/
    # absent-binary yields None so detect() and the fail-soft reads keep treating an
    # unreachable bridge as "no token", never a crash.
    account = str(config.instance_value("devices.firewalla.keychain.account", ""))
    service = str(
        config.instance_value("devices.firewalla.keychain.service", _FIREWALLA_KEYCHAIN_SERVICE)
    )
    return creds_resolver.resolve_secret_optional(account=account, service=service)


def _resolve_ssh_key() -> str | None:
    """Resolve the durable Firewalla SSH key path from instance.yaml.

    Reads ``firewalla.ssh_key`` (discovery-first, mirroring
    :func:`sanctum_cli.commands.net._firewalla_key_path`); falls back to the
    legacy ``~/.ssh/firewalla_ed25519`` layout. Returns the path as a string
    when it exists on disk, else ``None`` (the SSH transport is simply
    unavailable — reads/policies still work over the bridge).
    """
    configured = config.instance_value("firewalla.ssh_key", None)
    candidate = (
        Path(str(configured)).expanduser()
        if configured
        else Path.home() / ".ssh" / "firewalla_ed25519"
    )
    return str(candidate) if candidate.exists() else None


def _fetch_bridge_json(
    path: str, *, url: str | None = None, token: str | None = None
) -> dict[str, Any] | None:
    """GET a bridge endpoint with the bearer token; ``None`` on any failure.

    Fail-soft by design (mirrors the screen_time engine): a non-200, non-JSON,
    non-dict body, missing token, or transport error all return ``None`` so a
    caller treats an unreachable bridge as "no data", never a crash. This is the
    seam tests monkeypatch so no socket is opened.

    ``url`` / ``token`` default to this provider's own env/on-disk resolution
    (``_bridge_url`` / ``_read_bridge_token``). They may be passed explicitly so
    a consumer that resolves the same transport from its *own* config (e.g. the
    screen-time engine, which routes its bridge reads through this seam) drives
    the read without re-resolving here — keeping a single HTTP implementation.
    """
    bearer = token if token is not None else _read_bridge_token()
    if not bearer:
        return None
    base = url if url is not None else _bridge_url()
    # Own the encoding at the boundary (exactly once); httpx then has nothing left
    # to encode and cannot double-encode a literal '%' to '%2525'.
    safe_path = _encode_path(path)
    try:
        with httpx.Client(transport=_bridge_transport(), timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(
                f"{base}{safe_path}",
                headers={"Authorization": f"Bearer {bearer}"},
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def _get_bridge_json_strict(path: str) -> dict[str, Any] | None:
    """GET a bridge endpoint, distinguishing "no data" from "transport/auth down".

    The contract-honoring counterpart to the fail-soft :func:`_fetch_bridge_json`.
    The Protocol (``base.DeviceProvider.get``) mandates two DIFFERENT signals for
    two DIFFERENT facts, and the fail-soft seam collapses them into one ``None``:

    * a path the box has **no body** for — a genuine 404, or a 200 carrying an
      empty / non-dict body → ``None`` (best-effort "unknown path", per contract);
    * the bridge is **unreachable** (transport error), the token is **missing**,
      or the box **rejected the token** (401/403) or otherwise errored (any other
      non-200) → raise :class:`DeviceError`.

    Without this, a dead bridge or a rejected token is indistinguishable from
    "box up, empty body" — ``get`` returns ``None`` and ``firewalla_status``
    prints ``info: -`` and exits 0 on a total connectivity / auth failure, the
    opposite signal the Sagemcom provider gives for the same failure class
    (``_raw_get`` normalizes any transport error to ``DeviceError``). This seam
    is module-level so tests can monkeypatch it without opening a socket; the
    fail-soft :func:`_fetch_bridge_json` is retained UNCHANGED for the callers
    that must never raise (``detect`` during a registry scan, the best-effort
    ``connect``/``_refine_brand``/``snapshot`` baseline).
    """
    bearer = _read_bridge_token()
    if not bearer:
        msg = "Firewalla bridge token unavailable (no FIREWALLA_BRIDGE_TOKEN env / on-disk secret)"
        raise DeviceError(
            msg,
            fix=(
                "set FIREWALLA_BRIDGE_TOKEN or write the token to "
                "~/.sanctum/secrets/firewalla-bridge-token"
            ),
        )
    base = _bridge_url()
    safe_path = _encode_path(path)
    try:
        with httpx.Client(transport=_bridge_transport(), timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(
                f"{base}{safe_path}",
                headers={"Authorization": f"Bearer {bearer}"},
            )
    except httpx.HTTPError as exc:  # transport down: unreachable / timeout / reset
        msg = f"Firewalla bridge unreachable for GET {path!r}: {exc}"
        raise DeviceError(
            msg, fix="check the bridge is up (FIREWALLA_BRIDGE_URL, default 127.0.0.1:1984)"
        ) from exc
    if resp.status_code in (401, 403):  # the box rejected the bearer token
        msg = f"Firewalla bridge rejected the token for GET {path!r} (HTTP {resp.status_code})"
        raise DeviceError(msg, fix="rotate / re-provision the Firewalla bridge bearer token")
    if resp.status_code == 404:  # path genuinely unknown → best-effort None, per contract
        return None
    if resp.status_code != 200:  # any other server-side error is a transport-class failure
        msg = f"Firewalla bridge errored for GET {path!r} (HTTP {resp.status_code})"
        raise DeviceError(msg, fix="check the bridge logs; the box returned an unexpected status")
    try:
        data = resp.json()
    except ValueError:
        # 200 with a non-JSON body: the path answered but carries no structured
        # value — best-effort "no data", not a transport failure.
        return None
    return data if isinstance(data, dict) else None


def _post_bridge_json(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """POST a JSON body to a bridge endpoint; ``None`` on any failure.

    The mutating counterpart to :func:`_fetch_bridge_json`, used by ``set`` and
    the policy ``rollback`` restore. Same fail-soft contract: a non-2xx, missing
    token, or transport error returns ``None`` so the caller reports
    ``ok=False`` rather than raising mid-mutation. Tests monkeypatch this so no
    write ever reaches live gear.
    """
    token = _read_bridge_token()
    if not token:
        return None
    # Own the encoding at the boundary (exactly once) — same contract as the GET
    # seam: a '%'-bearing policy id in a mutate path must address the right policy.
    safe_path = _encode_path(path)
    try:
        with httpx.Client(transport=_bridge_transport(), timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.post(
                f"{_bridge_url()}{safe_path}",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
        if resp.status_code // 100 != 2:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def _delete_bridge_json(path: str) -> dict[str, Any] | None:
    """DELETE a bridge endpoint; ``None`` on any failure.

    The DELETE counterpart to :func:`_post_bridge_json`, used by the
    ``DELETE /policy/:pid`` and ``DELETE /dns/:hostname`` ops. Same fail-soft
    contract: a non-2xx (the bridge answers ``500 {success:false}`` when a delete
    fails), a missing token, or a transport error returns ``None`` so the caller
    reports ``ok=False`` rather than raising mid-mutation. The bridge DELETE routes
    carry NO request body (see ``firewalla-bridge.js`` + the production
    ``screen_time._bridge_request``, which sends no ``json`` on DELETE), so none is
    sent. Tests monkeypatch the transport so no write ever reaches live gear.
    """
    token = _read_bridge_token()
    if not token:
        return None
    # Own the encoding at the boundary (exactly once) — a '%'-bearing id in the
    # mutate path must address the right record, not a server-side mis-decode.
    safe_path = _encode_path(path)
    try:
        with httpx.Client(transport=_bridge_transport(), timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.delete(
                f"{_bridge_url()}{safe_path}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code // 100 != 2:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def _ssh_port_open() -> bool:
    """Read-only fingerprint: is ``firewalla.local:22`` reachable?

    A pure TCP-connect probe (no auth, no command) used by ``detect`` as the
    SSH-side evidence the box is present. Tests monkeypatch this so ``detect``
    never opens a socket.
    """
    try:
        socket.create_connection((_SSH_HOST, _SSH_PORT), timeout=_SSH_PROBE_TIMEOUT_S).close()
    except OSError:
        return False
    return True


# Caps whose bridge routes exist regardless of the box's enforcement mode — reads,
# box-level ops, and local-DNS behave the same in router / spoof / dhcp mode.
_BASE_CAPS: frozenset[Capability] = frozenset(
    {
        Capability.READ,
        Capability.LOCAL_DNS,
        Capability.ALARM_ACK,
        Capability.WAKE_ON_LAN,
        Capability.SPEEDTEST,
        Capability.REBOOT,
    }
)
# Caps that only ENFORCE when the box is enforcement-ready (router mode). A per-device
# block / policy / rule, a feature toggle, or a screen-time pause installs over the
# bridge in any mode, but on a box that is NOT enforcement-ready the write does not
# reliably take effect — so /info's ``enforcement_ready`` gates whether they are
# advertised (an EXPLICIT ``false`` strips them; unknown keeps them — the routes exist).
_ENFORCEMENT_CAPS: frozenset[Capability] = frozenset(
    {
        Capability.POLICY,
        Capability.SCREEN_TIME,
        Capability.DEVICE_BLOCK,
        Capability.DEVICE_POLICY,
        Capability.DEVICE_RULES,
        Capability.FEATURE_TOGGLE,
    }
)

# The honest cap → (transport, concrete bridge route) map. Each row names the REAL
# route-correct op the matching named method issues (verified against the named
# ops below), so a binding cannot outlive its route. ``build_capability_map`` raises
# if ``capabilities()`` ever advertises a cap with no row here. WAN_MODE is absent
# BY DESIGN — NAT/DMZ/WAN/VPN are GUI-only on a Firewalla (the bridge proxies no
# such route), so they live in the ceiling, never as a phantom binding.
_CAP_BINDINGS: dict[Capability, tuple[str, str]] = {
    Capability.READ: ("bridge-http", "GET /info, /policies, /host/:mac, /hosts"),
    Capability.LOCAL_DNS: ("bridge-http", "POST /dns ; DELETE /dns/:hostname"),
    Capability.ALARM_ACK: ("bridge-http", "POST /alarm/:id/ignore"),
    Capability.WAKE_ON_LAN: ("bridge-http", "POST /host/:mac/wake"),
    Capability.SPEEDTEST: ("bridge-http", "POST /speedtest"),
    Capability.REBOOT: ("bridge-http", "POST /box/reboot"),
    Capability.POLICY: ("bridge-http", "DELETE /policy/:pid ; POST /raw policy:create"),
    Capability.SCREEN_TIME: ("bridge-http", "POST /host/:mac/pause|unpause"),
    Capability.DEVICE_BLOCK: ("bridge-http", "POST /host/:mac/pause|unpause"),
    Capability.DEVICE_POLICY: ("bridge-http", "POST /host/:mac/policy"),
    Capability.DEVICE_RULES: ("bridge-http", "POST /host/:mac/rules"),
    Capability.FEATURE_TOGGLE: ("bridge-http", "POST /feature/:name/enable|disable"),
}

# The GUI-only ceiling: the WAN/edge surfaces the bridge proxies NO route for, named
# so a caller is TOLD the wall rather than discovering it by a failed call.
_GUI_ONLY_CEILING: tuple[str, ...] = (
    "NAT configuration (bridge proxies no route — Firewalla app only)",
    "DMZ host (no bridge route — app only)",
    "WAN mode / WAN settings (WAN_MODE: no bridge route — app only)",
    "VPN server/client (no bridge route — app only)",
)


class FirewallaProvider:
    """Layer-1 control surface for a Firewalla box (bridge HTTP + durable SSH)."""

    kind = "firewalla"
    brand = "firewalla"

    def __init__(self) -> None:
        self._connected = False
        self._token: str | None = None
        self._key_path: str | None = None

    @staticmethod
    def detect(net: NetContext) -> float:  # noqa: ARG004 - gateway not needed for the probe
        """Confidence this provider drives the gear at ``net`` (read-only probes).

        Two independent, read-only signals, strongest first:

        * the bridge ``/info`` answers (token-authed, the canonical "the box is
          here and paired" evidence) → ``1.0``; else
        * ``firewalla.local:22`` accepts a TCP connection (the box is present
          but the bridge is down / unconfigured) → ``0.5`` partial; else
        * neither → ``0.0``.

        No credentials beyond the already-on-disk bridge token, and no mutation
        — safe to call during a registry scan.
        """
        if _fetch_bridge_json(_INFO_PATH) is not None:
            return 1.0
        if _ssh_port_open():
            return 0.5
        return 0.0

    def connect(self, creds: Creds | None) -> None:  # noqa: ARG002 - creds optional; self-resolves
        """Resolve the bridge token + SSH key and refine ``brand`` from ``/info``.

        ``creds`` is optional — the provider self-resolves its transports from
        the environment (bearer token via env / on-disk secret, SSH key via
        instance.yaml), so credentials never have to flow through the CLI layer.
        Probing ``/info`` is best-effort: an unreachable bridge leaves ``brand``
        as the generic ``firewalla`` rather than failing the connect (reads and
        policies degrade to ``None``, which the rest of the surface tolerates).
        """
        self._token = _read_bridge_token()
        self._key_path = _resolve_ssh_key()
        self._connected = True
        self._refine_brand()

    def _refine_brand(self) -> None:
        """Best-effort: turn ``firewalla`` into ``firewalla-<model>`` post-connect."""
        info = _fetch_bridge_json(_INFO_PATH)
        if not info:
            return
        box = info.get("box") or {}
        model = box.get("model")
        if model:
            self.brand = f"firewalla-{str(model).lower()}"

    def disconnect(self) -> None:
        """Release the (HTTP/SSH) transport state. Idempotent + safe unconnected.

        No long-lived socket is held (the bridge is per-request HTTP and the SSH
        transport is invoked per-op), so teardown just drops the resolved token /
        key and the connected flag. Safe to call when never connected and safe to
        call twice — the uniform lifecycle-close the Protocol mandates.
        """
        self._connected = False
        self._token = None
        self._key_path = None

    def _require_connected(self) -> None:
        if not self._connected:
            msg = "Firewalla not connected; call connect() first"
            raise DeviceError(msg, fix="call provider.connect(creds) before reading/writing")

    def get(self, path: str) -> str | None:
        """Read one bridge endpoint by path; ``None`` only when the box has no body.

        Honors the :class:`~sanctum_cli.devices.base.DeviceProvider` Protocol
        contract: returns the JSON payload serialized to a compact string, or
        ``None`` for a path the box genuinely has no body for (a 404, or a 200
        with an empty / non-dict / non-JSON body). A transport failure, a missing
        token, or an auth-reject (401/403) raises :class:`DeviceError` rather than
        masquerading as an empty body — so a dead bridge / rejected token is
        distinguishable from "box up, empty endpoint" (matching Sagemcom's
        ``get``, which also raises on transport/auth failure). Routed through the
        strict seam; the fail-soft ``_fetch_bridge_json`` is reserved for the
        best-effort callers (detect / connect / snapshot) that must never raise.
        """
        self._require_connected()
        data = _get_bridge_json_strict(path)
        if data is None:
            return None
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    def set(self, path: str, value: str) -> OpResult:
        """Write to a bridge route, the body parsed from ``value`` (route-correct JSON).

        The Firewalla bridge exposes NO generic single-value route — EVERY mutate is
        a *structured* POST (per-device policy/rules, feature toggle, box op) keyed on
        named body fields. The prior code wrapped the value in ``{"value": ...}``, a
        shape that matches almost no bridge route, so the write silently addressed
        nothing. So the Protocol's ``set(path, value)`` now interprets ``value`` as the
        JSON request body the target route expects (e.g.
        ``set("/host/<mac>/policy", '{"family": 1}')``) and rides the SAME route-correct
        POST as :meth:`op`. A ``value`` that is not a JSON object yields ``ok=False``
        with a legible detail (no phantom ``{"value": ...}`` write), never a green
        result on a route that ignored the body. Prefer the named ops
        (:meth:`device_policy` / :meth:`device_block` / …) — they own the route + body
        so a caller never hand-builds either.
        """
        self._require_connected()
        try:
            body = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return OpResult(
                ok=False,
                detail=(
                    f"set {path}: value must be a JSON body for the bridge route "
                    "(the bridge has no generic {\"value\": …} route)"
                ),
            )
        if not isinstance(body, dict):
            return OpResult(ok=False, detail=f"set {path}: body must be a JSON object")
        return self.op(path, body)

    def op(self, path: str, body: dict[str, Any] | None = None) -> OpResult:
        """Generic route-correct ``POST`` escape hatch onto ANY bridge POST route.

        The Firewalla counterpart to Sagemcom's ``action`` escape hatch: it POSTs an
        arbitrary, caller-shaped JSON ``body`` to ``path`` so every bridge POST route
        — ``/host/:mac/policy|rules|pause|unpause|wake``, ``/feature/:name/enable``,
        ``/dns``, ``/alarm/:id/ignore``, ``/box/*``, ``/speedtest`` — is reachable
        with the body that route actually reads (NOT the prior ``{"value": …}`` wrapper
        that matched none). ``body=None`` posts ``{}`` (the bodyless routes, matching the
        production caller's ``json=(data or {})``). A bridge that refuses (``None`` from
        the POST seam — non-2xx / missing token / transport error) yields ``ok=False``
        rather than raising, so the rails trip rollback. Composed behind
        ``guarded_apply`` at the intent layer; never auto-fired.
        """
        self._require_connected()
        before_data = _fetch_bridge_json(path)
        before = (
            json.dumps(before_data, separators=(",", ":"), ensure_ascii=False)
            if before_data is not None
            else None
        )
        result = _post_bridge_json(path, body or {})
        if result is None:
            return OpResult(ok=False, detail=f"bridge refused POST {path}", before=before)
        after = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        return OpResult(ok=True, detail=f"POST {path}", before=before, after=after)

    def raw(
        self,
        msg_type: str,
        item: str,
        *,
        value: dict[str, Any] | None = None,
        target: str | None = None,
    ) -> OpResult:
        """Generic escape hatch — ``POST /raw`` ``{type,item,value,target}``.

        The Firewalla counterpart to Sagemcom's ``action`` escape hatch: it reaches
        the bridge's ``POST /raw`` route, which builds a raw ``FWMessage(type,
        {item, value}, target)`` and sends it straight to the box — so ANY SDK verb
        the named ops do not wrap (a ``policy:create``, a fresh ``get`` the box
        exposes, a future feature message) is reachable without a provider change.
        ``msg_type`` and ``item`` are REQUIRED by the route (the bridge 400s without
        both, so they are positional); ``value`` (box-side default ``{}``) and
        ``target`` (box-side default ``"0.0.0.0"``) are OMITTED from the body when the
        caller leaves them unset, so the bridge fills its own defaults rather than the
        provider sending a field it never meant to specify. Rides the SAME
        route-correct POST as :meth:`op`, so a bridge refusal yields ``ok=False``
        (never a phantom success); composed behind ``guarded_apply`` at the intent
        layer — never auto-fired.
        """
        body: dict[str, Any] = {"type": msg_type, "item": item}
        if value is not None:
            body["value"] = value
        if target is not None:
            body["target"] = target
        return self.op("/raw", body)

    def _recreate_policy(self, policy: dict[str, Any]) -> OpResult:
        """Re-create a policy removed since the snapshot — ``POST /raw`` ``policy:create``.

        The restore half that the bridge has no dedicated route for: it mirrors the
        bridge's OWN policy-creation path (``sendCmd("policy:create", …)`` =
        ``FWCmdMessage``), reached here through the generic :meth:`raw` escape hatch as
        a ``cmd``-type message carrying the captured policy object as its value. The
        box assigns a fresh pid on creation (the captured ``pid`` is advisory), so the
        captured policy rides verbatim as the create payload.
        """
        return self.raw("cmd", "policy:create", value=policy)

    def _delete_op(self, path: str) -> OpResult:
        """Generic route-correct ``DELETE`` for ``/policy/:pid`` + ``/dns/:hostname``.

        The DELETE sibling of :meth:`op`; the bridge DELETE routes carry no body. A
        refusal (the bridge answers ``500 {success:false}`` on a failed delete →
        ``None`` from the seam) yields ``ok=False`` so the rails surface the failure.
        """
        self._require_connected()
        result = _delete_bridge_json(path)
        if result is None:
            return OpResult(ok=False, detail=f"bridge refused DELETE {path}")
        after = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        return OpResult(ok=True, detail=f"DELETE {path}", after=after)

    # ── named ops: each owns its route + route-correct body ───────────────

    def device_block(self, mac: str, *, blocked: bool) -> OpResult:
        """Pause (``blocked=True``) or unpause a device — ``POST /host/:mac/{un}pause``.

        The bridge installs/removes a MAC-level block rule (it reads no request body;
        the production ``screen_time`` caller posts none, so ``op`` sends ``{}``).
        """
        verb = "pause" if blocked else "unpause"
        return self.op(f"/host/{mac}/{verb}")

    def device_policy(
        self,
        mac: str,
        *,
        family: bool | None = None,
        adblock: bool | None = None,
        safe_search: bool | dict[str, Any] | None = None,
        ip_allocation: dict[str, Any] | None = None,
    ) -> OpResult:
        """Set per-device policy — ``POST /host/:mac/policy`` (family/adblock/safeSearch/dhcp).

        Only the fields the caller supplies are sent (the bridge keys on ``"k" in body``),
        under the bridge's own camelCase keys: ``family``, ``adblock``, ``safeSearch``,
        and ``ipAllocation`` (the dhcp-reservation object). Calling with NO field set is a
        no-op — it fires no phantom POST and reports ``ok=False`` legibly.
        """
        body: dict[str, Any] = {}
        if family is not None:
            body["family"] = family
        if adblock is not None:
            body["adblock"] = adblock
        if safe_search is not None:
            body["safeSearch"] = safe_search
        if ip_allocation is not None:
            body["ipAllocation"] = ip_allocation
        if not body:
            return OpResult(
                ok=False,
                detail=f"device_policy {mac}: no policy field set (nothing to write)",
            )
        return self.op(f"/host/{mac}/policy", body)

    def device_rules(
        self,
        mac: str,
        services: list[str],
        *,
        action: str = "block",
        expire: int | None = None,
    ) -> OpResult:
        """Block/allow services on a device — ``POST /host/:mac/rules``.

        ``services`` is a list of service names (the bridge expands them to domains);
        ``action`` is ``"block"`` / ``"allow"``; ``expire`` is a unix timestamp at which
        the rule auto-deletes on the box (omitted from the body when ``None`` — but always
        pass it: an unbounded rule was the 2026-04-18 stale-rule root cause). Body shape
        is verified against the production ``screen_time._block_services`` caller.
        """
        body: dict[str, Any] = {"services": services, "action": action}
        if expire is not None:
            body["expire"] = expire
        return self.op(f"/host/{mac}/rules", body)

    def feature_toggle(self, name: str, *, enabled: bool) -> OpResult:
        """Enable/disable a global box feature — ``POST /feature/:name/{enable,disable}``."""
        verb = "enable" if enabled else "disable"
        return self.op(f"/feature/{name}/{verb}")

    def local_dns_set(self, hostname: str, ip: str) -> OpResult:
        """Add/update a local DNS record — ``POST /dns`` ``{hostname, ip}``."""
        return self.op("/dns", {"hostname": hostname, "ip": ip})

    def local_dns_delete(self, hostname: str) -> OpResult:
        """Remove a local DNS record — ``DELETE /dns/:hostname``."""
        return self._delete_op(f"/dns/{hostname}")

    def delete_policy(self, pid: int) -> OpResult:
        """Delete a policy by id — ``DELETE /policy/:pid``."""
        return self._delete_op(f"/policy/{pid}")

    def alarm_ack(self, alarm_id: str) -> OpResult:
        """Acknowledge / dismiss an alarm — ``POST /alarm/:id/ignore``."""
        return self.op(f"/alarm/{alarm_id}/ignore")

    def wake_on_lan(self, mac: str) -> OpResult:
        """Send a Wake-on-LAN magic packet to a device — ``POST /host/:mac/wake``."""
        return self.op(f"/host/{mac}/wake")

    def speedtest(self) -> OpResult:
        """Run a new speedtest on the box — ``POST /speedtest``."""
        return self.op("/speedtest")

    # ── box ops ───────────────────────────────────────────────────────────

    def box_reboot(self) -> OpResult:
        """Reboot the box — ``POST /box/reboot`` (network down ~2-3 min)."""
        return self.op("/box/reboot")

    def box_shutdown(self) -> OpResult:
        """Shut the box down — ``POST /box/shutdown`` (security down until restarted)."""
        return self.op("/box/shutdown")

    def box_shutdown_cancel(self) -> OpResult:
        """Cancel a pending shutdown — ``POST /box/shutdown/cancel``."""
        return self.op("/box/shutdown/cancel")

    def box_upgrade(self) -> OpResult:
        """Trigger a firmware upgrade — ``POST /box/upgrade``."""
        return self.op("/box/upgrade")

    def reboot(self) -> OpResult:
        """Protocol-aligned reboot — delegates to :meth:`box_reboot`.

        The :mod:`sanctum_cli.devices.intents` reboot seam duck-types a callable
        ``reboot()`` returning an :class:`OpResult`; this aliases the box reboot so a
        Firewalla can stand in wherever the intent layer drives a hardware reboot.
        """
        return self.box_reboot()

    def capabilities(self) -> AbstractSet[Capability]:
        """Operations this box actually supports — data-driven from ``GET /info``.

        Honest-verify on two axes:

        * every advertised cap is backed by a REAL, route-correct op on this provider
          (the method that POSTs/DELETEs the matching bridge route); and
        * the enforcement-class caps (per-device block / policy / rules, feature
          toggles, screen-time) are advertised only when ``/info`` reports the box is
          ``enforcement_ready`` (router mode). On a box NOT in router mode those policy
          writes install but do not reliably ENFORCE, so advertising them would
          over-promise — an EXPLICIT ``capabilities.enforcement_ready == false`` from
          the box strips them. When ``/info`` is unreachable or omits the flag (older
          bridge, transient down) the enforcement state is UNKNOWN and the ops' routes
          still exist, so they are kept — only an explicit ``false`` shrinks the
          surface, never a momentary read failure.

        ``WAN_MODE`` is NEVER advertised — NAT/DMZ/WAN-mode are GUI-only on a Firewalla
        (the bridge proxies no such route), so a ``WAN_MODE`` cap would name an op that
        does not exist (it stays a real cap for the Sagemcom hub, which DOES back it).
        """
        caps: set[Capability] = set(_BASE_CAPS)
        info = _fetch_bridge_json(_INFO_PATH)
        info_caps = info.get("capabilities") if isinstance(info, dict) else None
        enforcement_ready = (
            info_caps.get("enforcement_ready") if isinstance(info_caps, dict) else None
        )
        if enforcement_ready is not False:
            caps |= _ENFORCEMENT_CAPS
        return caps

    def capability_op(self, capability: Capability) -> CapabilityOp | None:  # noqa: ARG002
        """No brand-specific (path, engaged) binding is exposed.

        Firewalla policy/screen-time ops are driven imperatively through ``set``
        / the bridge POST seam (a policy id + action), not through a single
        leaf-and-engaged-value mapping the way a TR-069 hub's bridge-mode is. So
        the brand-vocabulary seam returns ``None`` for every capability — intents
        targeting Firewalla compose ``set`` directly rather than reading a
        ``CapabilityOp``.
        """
        return None

    def capability_map(self) -> CapabilityMap:
        """Honest "what can I change on this box": real bridge routes + the GUI ceiling.

        Every cap :meth:`capabilities` advertises (which is itself data-driven from
        ``/info``'s ``enforcement_ready``) is bound to the concrete route-correct
        bridge op that backs it; so when the box is NOT enforcement-ready and the
        enforcement caps drop, their bindings drop with them — the map tracks the
        live surface. The ceiling names NAT/DMZ/WAN/VPN, the GUI-only edge surfaces
        the bridge proxies no route for, so WAN_MODE is reported as a wall, never as
        a phantom op. ``build_capability_map`` enforces bindings ≡ ``capabilities()``.
        """
        return build_capability_map(
            brand=self.brand,
            capabilities=self.capabilities(),
            bindings=_CAP_BINDINGS,
            ceiling=_GUI_ONLY_CEILING,
        )

    def list_paths(self) -> list[CapabilityBinding]:
        """The flat list of REAL bridge-route bindings live on this box right now."""
        return list(self.capability_map().bindings)

    def snapshot(self, scope: str | None = None) -> Snapshot:  # noqa: ARG002 - whole policy subtree
        """Capture the box's policy state as the restorable rollback baseline.

        A best-effort read of ``/policies``; the serialized payload is stored
        under the ``/policies`` key so :meth:`rollback` can re-apply it. An
        unreachable bridge yields an empty snapshot — and :meth:`rollback`
        reports that empty baseline as a FAILED restore (never a silent
        success), so a pause that could not be captured cannot be falsely
        "rolled back".
        """
        data: dict[str, str] = {}
        policies = _fetch_bridge_json(_POLICIES_PATH)
        if policies is not None:
            data[_POLICIES_PATH] = json.dumps(
                policies, separators=(",", ":"), ensure_ascii=False
            )
        return Snapshot(
            brand=self.brand,
            taken_at=datetime.now(tz=UTC).isoformat(),
            data=data,
        )

    def rollback(self, snap: Snapshot) -> OpResult:
        """Restore the captured policy baseline via the bridge's REAL primitives.

        Re-based off the routes that ACTUALLY exist (the prior code POSTed a
        non-existent ``POST /policies/restore`` — a silent no-op that, against a
        catch-all, could even report a green restore that never happened). The diff
        between the captured ``/policies`` baseline and the LIVE state is reconciled:

        * a policy present in the live state but NOT in the baseline was ADDED since
          the snapshot → removed with ``DELETE /policy/:pid``;
        * a policy in the baseline but gone from the live state was REMOVED since the
          snapshot → re-created with the ``POST /raw`` ``policy:create`` escape hatch.

        Fail-closed throughout — an absent baseline, an unreadable live state, OR any
        primitive that reports ``ok=False`` makes the WHOLE rollback ``ok=False`` so
        the rails surface a manual-recovery instruction instead of a false success. A
        live state already equal to the baseline is a successful restore that mutates
        nothing (``ok=True``).
        """
        self._require_connected()
        captured = snap.data.get(_POLICIES_PATH)
        if not captured:
            return OpResult(
                ok=False,
                detail="rollback failed: snapshot carried no restorable policy baseline",
            )
        try:
            baseline_raw = json.loads(captured)
        except (json.JSONDecodeError, TypeError):
            return OpResult(
                ok=False,
                detail="rollback failed: snapshot policy baseline is not valid JSON",
            )
        baseline = _policies_by_pid(baseline_raw if isinstance(baseline_raw, dict) else None)

        live = _fetch_bridge_json(_POLICIES_PATH)
        if live is None:
            return OpResult(
                ok=False,
                detail=(
                    "rollback failed: could not read the live policy state to reconcile "
                    "against the baseline; restore manually in the Firewalla app"
                ),
            )
        current = _policies_by_pid(live)

        # Added since the snapshot → delete; removed since the snapshot → re-create.
        to_delete = [pid for pid in current if pid not in baseline]
        to_recreate = [pol for pid, pol in baseline.items() if pid not in current]

        failures: list[str] = []
        for pid in to_delete:
            if not self._delete_op(f"/policy/{pid}").ok:
                failures.append(f"delete pid={pid}")
        for pol in to_recreate:
            if not self._recreate_policy(pol).ok:
                failures.append(f"recreate pid={pol.get('pid')}")

        if failures:
            return OpResult(
                ok=False,
                detail=(
                    "rollback FAILED (fail-closed): "
                    + ", ".join(failures)
                    + "; restore the remaining policy state manually in the Firewalla app"
                ),
            )
        return OpResult(
            ok=True,
            detail=(
                f"rollback restored policy baseline "
                f"(deleted {len(to_delete)} added, re-created {len(to_recreate)} removed)"
            ),
        )


# Self-register on import so the registry resolves ``firewalla`` to this provider.
registry.register(FirewallaProvider)
