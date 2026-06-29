"""HA Green provider — the Home Assistant appliance on the DeviceProvider contract.

The haus runs a **Home Assistant Green** (HAOS appliance) at a static LAN IP
(``10.0.0.3``, a Firewalla DHCP reservation; MAC ``20:F8:3B:02:3A:C8``). It is
driven through the uniform :class:`~sanctum_cli.devices.base.DeviceProvider`
surface exactly like the Firewalla box — a **Bearer-token HTTP** transport — so
``sanctum net ha-green`` and the ``sanctum onboard`` HA-Green chapter read it the
same way every other brand is read.

ACCESS MODEL (encoded honestly — this is the load-bearing nuance):

* the **REST API** at ``http://10.0.0.3:8123`` accepts the long-lived *owner*
  token (``Authorization: Bearer <token>``). ``GET /api/`` returns
  ``{"message": "API running."}`` — the canonical "Core is up and the token is
  good" oracle this provider verifies against; ``GET /api/config`` carries the
  Core ``version`` (used to refine ``brand`` to ``ha-green-<version>``).
* the REST **Supervisor proxy** (``/api/hassio/*``) REJECTS the owner token
  (HTTP 401). Only the WebSocket ``supervisor/api`` proxy accepts it — and that
  WS surface is owned by the ``ha-green-toolkit`` (``hag-ws.py``), NOT this
  provider. So a strict read of a ``/api/hassio/*`` path here HONESTLY raises a
  :class:`DeviceError` (token rejected) rather than pretending the path is empty.
* there is **NO host SSH** to a Green — unlike the Firewalla, this provider holds
  only the one HTTP transport (no SSH seam).

The owner token is resolved fresh on every connect (env ``HA_GREEN_TOKEN`` →
on-disk ``~/.sanctum/secrets/ha-token``); it is NEVER persisted by this module.
Reads are best-effort where a miss is normal (``detect`` / ``connect`` /
``_refine_brand`` fail-soft to ``None`` / generic brand) and STRICT where the
Protocol mandates distinguishing "no body" from "transport/auth down" (``get``).

Remote access rides a **Tailscale add-on** that joins the tailnet as node
``homeassistant`` (``tag:sanctum-host``), reachable at
``http://homeassistant.tail7c6d11.ts.net:8123``. :func:`tailscale_node_present`
is a read-only fingerprint (one ``tailscale status`` shell-out) the status
surface + onboard chapter report.

This surface is **read-only** in sanctum-cli: HA state mutations + add-on control
go through the toolkit's WS path, so the provider advertises only
:attr:`Capability.READ` and ``set``/``snapshot``/``rollback`` are honest read-only
no-ops (never auto-fired through the rails). Every transport seam
(``_fetch_api_json`` / ``_get_api_json_strict`` / ``_port_open`` /
``_tailscale_status_text`` / the token resolver) is module-level so tests can
monkeypatch them and never open a socket; a live read-only smoke is env-gated
behind ``SANCTUM_LIVE_HA_GREEN=1`` (see ``tests/devices/test_ha_green.py``).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

from sanctum_cli.devices import registry
from sanctum_cli.devices.base import (
    Capability,
    CapabilityOp,
    Creds,
    DeviceError,
    NetContext,
    OpResult,
    Snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

# REST transport config. The owner token rides as a Bearer header; the on-disk
# secret path is the documented ``~/.sanctum/secrets/ha-token`` (the SAME single
# source of truth the ha-green-toolkit reads), env-overridable for a test / a
# second haus.
_HA_URL_ENV = "HA_GREEN_URL"
_HA_TOKEN_ENV = "HA_GREEN_TOKEN"
_HA_TOKEN_FILE = Path.home() / ".sanctum/secrets/ha-token"
_DEFAULT_HA_URL = "http://10.0.0.3:8123"

# The LAN fingerprint host:port (a DHCP reservation on the Firewalla). Used by the
# TCP-connect presence probe when the URL cannot be parsed; normally the host:port
# come from the resolved base URL so an HA_GREEN_URL override probes the right box.
_HA_HOST = "10.0.0.3"
_HA_PORT = 8123
_PORT_PROBE_TIMEOUT_S = 1.0

# REST read paths this surface reasons over.
_API_PATH = "/api/"
_CONFIG_PATH = "/api/config"

# The exact marker ``GET /api/`` returns when Core is up AND the token is good.
# The honest "HA is actually running" oracle — never inferred from "the request
# returned a 200" alone (a reverse proxy can 200 with an unrelated body).
_API_RUNNING_MESSAGE = "API running."

# Remote-access tailnet facts. The Tailscale add-on joins the tailnet as this node
# (``tag:sanctum-host``); the suffix is the haus tailnet's MagicDNS domain.
_TAILNET_NODE = "homeassistant"
_TAILNET_SUFFIX = "tail7c6d11.ts.net"

# HTTP timeout for REST calls (seconds).
_HTTP_TIMEOUT_S = 15

# RFC-3986 path-character safe set for boundary encoding — IDENTICAL to the
# Firewalla provider's (own the escaping at the boundary, exactly once; keep '/'
# separators + ':' + the other pchars literal, percent-encode a literal '%' to
# '%25' so a path whose id carries ``%41`` addresses the id's own bytes instead of
# being preserved by httpx and mis-decoded server-side to 'A'). See
# ``sanctum_cli.devices.firewalla._PATH_SAFE`` for the full rationale.
_PATH_SAFE = "/:@!$&'()*+,;=~-._"


def _ha_url() -> str:
    """Resolve the HA base URL (env override → the LAN default)."""
    return os.environ.get(_HA_URL_ENV, _DEFAULT_HA_URL)


def _encode_path(path: str) -> str:
    """Percent-encode a caller-supplied REST path EXACTLY once for the wire.

    The same boundary contract the Firewalla provider owns (CLAUDE.md "Own the
    escaping at the boundary"): ``quote(path, safe=_PATH_SAFE)`` turns a literal
    ``%`` into ``%25`` so httpx has nothing left to encode and cannot double-encode
    it, while keeping ``/`` separators and ``:`` and the other RFC-3986 path chars
    literal so a previously-working path rides byte-identical.
    """
    return quote(path, safe=_PATH_SAFE)


def _ha_transport() -> httpx.BaseTransport | None:
    """The httpx transport the REST client is built on (``None`` = default).

    A seam so tests can inject an ``httpx.MockTransport`` and drive the REAL httpx
    URL-construction / status / JSON parsing without opening a socket. In
    production it returns ``None`` so ``httpx.Client`` uses its own default transport.
    """
    return None


def _read_ha_token() -> str | None:
    """Resolve the owner token: env ``HA_GREEN_TOKEN`` → on-disk secret file.

    Returns ``None`` when no token is available anywhere (so a caller fail-softs
    instead of sending an unauthenticated probe HA would reject). The on-disk path
    is the documented ``~/.sanctum/secrets/ha-token``; its contents are stripped of
    trailing whitespace/newline.
    """
    token = os.environ.get(_HA_TOKEN_ENV, "").strip()
    if token:
        return token
    try:
        return _HA_TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _fetch_api_json(
    path: str, *, url: str | None = None, token: str | None = None
) -> dict[str, Any] | None:
    """GET a REST endpoint with the Bearer owner token; ``None`` on any failure.

    Fail-soft by design (mirrors the Firewalla bridge fetch): a non-200, non-JSON,
    non-dict body, missing token, or transport error all return ``None`` so a
    caller treats an unreachable / unauthorized HA as "no data", never a crash.
    This is the seam tests monkeypatch so no socket is opened, and the seam the
    best-effort callers (``detect`` / ``connect`` / ``_refine_brand`` /
    :func:`api_running` / :func:`ha_version`) route through.

    ``url`` / ``token`` default to this provider's own env/on-disk resolution so a
    caller can probe with an explicit just-entered token (the onboard pairing gate)
    before it lands on disk.
    """
    bearer = token if token is not None else _read_ha_token()
    if not bearer:
        return None
    base = url if url is not None else _ha_url()
    safe_path = _encode_path(path)
    try:
        with httpx.Client(transport=_ha_transport(), timeout=_HTTP_TIMEOUT_S) as client:
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


def _get_api_json_strict(path: str) -> dict[str, Any] | None:
    """GET a REST endpoint, distinguishing "no data" from "transport/auth down".

    The contract-honoring counterpart to the fail-soft :func:`_fetch_api_json` (the
    exact split the Firewalla provider makes). The Protocol (``base.DeviceProvider.get``)
    mandates two DIFFERENT signals for two DIFFERENT facts:

    * a path the box has **no body** for — a 404, or a 200 carrying an empty /
      non-dict / non-JSON body → ``None`` (best-effort "unknown path");
    * HA is **unreachable** (transport error), the token is **missing**, or HA
      **rejected the token** (401/403 — e.g. an owner token aimed at a
      ``/api/hassio/*`` Supervisor path) or otherwise errored (any other non-200)
      → raise :class:`DeviceError`.

    Without this, a powered-off Green or a rejected token is indistinguishable from
    "up, empty body" and ``ha_green status`` would print a dash and exit 0 on a
    total connectivity / auth failure. Module-level so tests monkeypatch it without
    a socket; the fail-soft :func:`_fetch_api_json` is reserved for the callers that
    must never raise.
    """
    bearer = _read_ha_token()
    if not bearer:
        msg = "HA Green owner token unavailable (no HA_GREEN_TOKEN env / on-disk secret)"
        raise DeviceError(
            msg,
            fix=(
                "set HA_GREEN_TOKEN or write the Home Assistant owner token to "
                "~/.sanctum/secrets/ha-token"
            ),
        )
    base = _ha_url()
    safe_path = _encode_path(path)
    try:
        with httpx.Client(transport=_ha_transport(), timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(
                f"{base}{safe_path}",
                headers={"Authorization": f"Bearer {bearer}"},
            )
    except httpx.HTTPError as exc:  # transport down: unreachable / timeout / reset
        msg = f"HA Green unreachable for GET {path!r}: {exc}"
        raise DeviceError(
            msg, fix="check the Green is powered + on the LAN (HA_GREEN_URL, default 10.0.0.3:8123)"
        ) from exc
    if resp.status_code in (401, 403):  # HA rejected the bearer token
        msg = f"HA Green rejected the token for GET {path!r} (HTTP {resp.status_code})"
        raise DeviceError(
            msg,
            fix=(
                "use the long-lived OWNER token (the Supervisor /api/hassio/* proxy "
                "rejects it — that path needs the WebSocket supervisor/api)"
            ),
        )
    if resp.status_code == 404:  # path genuinely unknown → best-effort None, per contract
        return None
    if resp.status_code != 200:  # any other server-side error is a transport-class failure
        msg = f"HA Green errored for GET {path!r} (HTTP {resp.status_code})"
        raise DeviceError(msg, fix="check the HA logs; Core returned an unexpected status")
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _url_host_port(url: str | None = None) -> tuple[str, int]:
    """Parse (host, port) from the resolved base URL; fall back to the LAN default.

    Keeps the TCP presence probe pointed at whatever box ``HA_GREEN_URL`` names (so
    a tailnet override probes the right host) without ever raising on a malformed
    URL — a parse miss yields the documented ``10.0.0.3:8123``.
    """
    parts = urlsplit(url if url is not None else _ha_url())
    host = parts.hostname or _HA_HOST
    port = parts.port or _HA_PORT
    return host, port


def _port_open() -> bool:
    """Read-only fingerprint: is the HA Green's ``host:port`` reachable on the LAN?

    A pure TCP-connect probe (no auth, no request) — the presence signal that
    distinguishes "the Green is powered + on the LAN" from "the token/API is the
    problem". Tests monkeypatch this so ``detect`` / :func:`lan_reachable` never
    open a socket.
    """
    host, port = _url_host_port()
    try:
        socket.create_connection((host, port), timeout=_PORT_PROBE_TIMEOUT_S).close()
    except OSError:
        return False
    return True


def lan_reachable() -> bool:
    """True iff the Green answers a TCP connect at its resolved ``host:port``.

    The honest LAN-presence row the ``net ha-green`` health report shows — derived
    from a REAL connect, never from "the command ran". A thin public alias over the
    :func:`_port_open` seam so callers (and tests) read intent, not the probe name.
    """
    return _port_open()


def api_running(*, url: str | None = None, token: str | None = None) -> bool:
    """True iff ``GET /api/`` returns 200 with ``{"message": "API running."}``.

    The load-bearing honest-verify primitive: Core is "up" ONLY when HA itself says
    so AND the owner token authenticated — never inferred from a bare 200 or a port
    being open. Fail-soft (routes through :func:`_fetch_api_json`), so a powered-off
    box / missing token / wrong body all return ``False`` (the status row + onboard
    chapter then read an honest "down", not a fake ✓). ``url`` / ``token`` may be
    passed so the onboard gate can verify a just-entered token before persisting it.
    """
    body = _fetch_api_json(_API_PATH, url=url, token=token)
    if not body:
        return False
    return body.get("message") == _API_RUNNING_MESSAGE


def ha_version(*, url: str | None = None, token: str | None = None) -> str | None:
    """Best-effort Core version from ``GET /api/config``; ``None`` when unavailable.

    Fail-soft (a down/unauthorized box → ``None``), so the version row is shown only
    when it was genuinely read. ``url`` / ``token`` default to the provider's own
    resolution.
    """
    body = _fetch_api_json(_CONFIG_PATH, url=url, token=token)
    if not body:
        return None
    version = body.get("version")
    return str(version) if version is not None else None


def _tailscale_status_text() -> str:
    """Read-only ``tailscale status`` text; ``""`` on any failure (a seam).

    Resolves the ``tailscale`` CLI from PATH, falling back to the macOS app bundle
    location, and returns its ``status`` stdout. Best-effort: a missing binary,
    a non-zero exit, or a timeout yields ``""`` (→ "node not found"), never a
    raise. Tests monkeypatch this so no real ``tailscale`` is run.
    """
    binary = shutil.which("tailscale") or (
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale").exists()
        else None
    )
    if binary is None:
        return ""
    try:
        proc = subprocess.run(
            [binary, "status"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return ""
    return proc.stdout


def _parse_tailscale_node(status_text: str, name: str) -> bool:
    """Pure parser: does ``tailscale status`` text list a peer named ``name``?

    ``tailscale status`` prints one peer per line as ``<ip> <hostname> <user>
    <os> ...``; the hostname is the second whitespace-delimited column. Match the
    column exactly (so ``homeassistant`` does not match a substring like
    ``homeassistant-old``). Pure so the column logic is unit-tested without a
    shell-out.
    """
    for line in status_text.splitlines():
        cols = line.split()
        if len(cols) >= 2 and cols[1] == name:
            return True
    return False


def tailscale_node_present(name: str = _TAILNET_NODE) -> bool:
    """True iff the tailnet lists a node named ``name`` (default ``homeassistant``).

    The honest remote-access row: derived from a real ``tailscale status`` read +
    an exact hostname-column match. Fail-soft — no tailscale / not joined → ``False``.
    """
    return _parse_tailscale_node(_tailscale_status_text(), name)


class HaGreenProvider:
    """Layer-1 control surface for a Home Assistant Green (REST owner-token HTTP)."""

    kind = "ha-green"
    brand = "ha-green"

    def __init__(self) -> None:
        self._connected = False
        self._token: str | None = None

    @staticmethod
    def detect(net: NetContext) -> float:  # noqa: ARG004 - gateway not needed for the probe
        """Confidence this provider drives the gear at ``net`` (read-only probes).

        Two independent, read-only signals, strongest first (mirroring Firewalla):

        * ``GET /api/`` answers with the running marker (token-authed — the
          canonical "the Green is here, up, and paired" evidence) → ``1.0``; else
        * the Green's ``host:port`` accepts a TCP connection (powered + on the LAN
          but the API is down / the token is missing) → ``0.5`` partial; else
        * neither → ``0.0``.

        No mutation and no credential beyond the already-on-disk owner token — safe
        to call during a registry scan.
        """
        if api_running():
            return 1.0
        if _port_open():
            return 0.5
        return 0.0

    def connect(self, creds: Creds | None) -> None:  # noqa: ARG002 - creds optional; self-resolves
        """Resolve the owner token and refine ``brand`` from ``/api/config``.

        ``creds`` is optional — the provider self-resolves its token from the
        environment (env → on-disk secret), so credentials never have to flow
        through the CLI layer. Probing ``/api/config`` is best-effort: an
        unreachable / unauthorized HA leaves ``brand`` as the generic ``ha-green``
        rather than failing the connect (reads then degrade per the strict ``get``
        contract). Mirrors the Firewalla provider's best-effort connect.
        """
        self._token = _read_ha_token()
        self._connected = True
        self._refine_brand()

    def _refine_brand(self) -> None:
        """Best-effort: turn ``ha-green`` into ``ha-green-<version>`` post-connect."""
        version = ha_version()
        if version:
            self.brand = f"ha-green-{version}"

    def disconnect(self) -> None:
        """Release the HTTP transport state. Idempotent + safe when never connected.

        No long-lived socket is held (REST is per-request HTTP), so teardown just
        drops the resolved token + the connected flag. Safe to call when never
        connected and safe to call twice — the uniform lifecycle-close the Protocol
        mandates.
        """
        self._connected = False
        self._token = None

    def _require_connected(self) -> None:
        if not self._connected:
            msg = "HA Green not connected; call connect() first"
            raise DeviceError(msg, fix="call provider.connect(creds) before reading")

    def get(self, path: str) -> str | None:
        """Read one REST endpoint by path; ``None`` only when HA has no body.

        Honors the :class:`~sanctum_cli.devices.base.DeviceProvider` Protocol
        contract: returns the JSON payload serialized to a compact string, or
        ``None`` for a path HA genuinely has no body for (a 404, or a 200 with an
        empty / non-dict / non-JSON body). A transport failure, a missing token, or
        an auth-reject (401/403 — e.g. an owner token aimed at a Supervisor
        ``/api/hassio/*`` path) raises :class:`DeviceError` rather than
        masquerading as an empty body (matching Firewalla's / Sagemcom's ``get``).
        Routed through the strict seam; the fail-soft ``_fetch_api_json`` is
        reserved for the best-effort callers (detect / connect / refine).
        """
        self._require_connected()
        data = _get_api_json_strict(path)
        if data is None:
            return None
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    def set(self, path: str, value: str) -> OpResult:  # noqa: ARG002 - read-only surface
        """Read-only surface: every write is refused with ``ok=False`` (never raises).

        HA state mutations + add-on control ride the WebSocket ``supervisor/api``
        path owned by the ``ha-green-toolkit`` (``hag-remote`` / ``hag-addon``), not
        this REST provider. So sanctum-cli advertises only :attr:`Capability.READ`
        and ``set`` returns a refused :class:`OpResult` (return-convention, like
        Orbi's unwritable-leaf path) — it is never auto-fired through the rails
        (capabilities gate it out first).
        """
        self._require_connected()
        return OpResult(
            ok=False,
            detail=(
                f"ha-green: read-only surface — refused set {path} "
                "(use ha-green-toolkit's WebSocket path for HA mutations)"
            ),
        )

    def capabilities(self) -> AbstractSet[Capability]:
        """The Green is a READ-only surface in sanctum-cli (mutations ride the WS path)."""
        return {Capability.READ}

    def capability_op(self, capability: Capability) -> CapabilityOp | None:  # noqa: ARG002
        """No brand-specific (path, engaged) binding — the surface mutates nothing."""
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:  # noqa: ARG002 - read-only surface
        """Capture nothing — a read-only surface has no restorable mutating state.

        Returns a stamped but empty :class:`Snapshot`; paired with :meth:`rollback`
        reporting an empty baseline as ``ok=False`` (never a silent success),
        mirroring the Firewalla provider's empty-snapshot contract.
        """
        return Snapshot(brand=self.brand, taken_at=datetime.now(tz=UTC).isoformat(), data={})

    def rollback(self, snap: Snapshot) -> OpResult:
        """No restorable baseline on a read-only surface → honest ``ok=False``.

        An empty rollback is NOT a success (it would falsely report a restore that
        did nothing). Since this surface never mutates, the snapshot always carries
        an empty baseline and rollback always reports a failed restore — the same
        honest signal Firewalla/Orbi give for an empty snapshot.
        """
        if not snap.data:
            return OpResult(
                ok=False,
                detail="rollback failed: ha-green is read-only — snapshot carried no baseline",
            )
        return OpResult(ok=False, detail="rollback failed: ha-green is a read-only surface")


# Self-register on import so the registry resolves ``ha-green`` to this provider.
registry.register(HaGreenProvider)
