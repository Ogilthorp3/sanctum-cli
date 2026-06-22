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

from sanctum_cli import config
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

# Bridge transport config — mirrors the existing screen_time engine so the two
# read the SAME box through the SAME token (single source of truth on disk).
_BRIDGE_URL_ENV = "FIREWALLA_BRIDGE_URL"
_BRIDGE_TOKEN_ENV = "FIREWALLA_BRIDGE_TOKEN"
_BRIDGE_TOKEN_FILE = Path.home() / ".sanctum/secrets/firewalla-bridge-token"
_DEFAULT_BRIDGE_URL = "http://127.0.0.1:1984"

# The durable key-auth host:port for the SSH fallback transport. The key path
# itself is resolved from instance.yaml (``firewalla.ssh_key``) — never
# hardcoded — so a contributor points it at their own box.
_SSH_HOST = "firewalla.local"
_SSH_PORT = 22
_SSH_PROBE_TIMEOUT_S = 1.0

# The policy subtree we snapshot before a mutating intent. A policy-state
# snapshot is the restorable baseline a pause/set rollback restores.
_POLICIES_PATH = "/policies"
_POLICIES_RESTORE_PATH = "/policies/restore"
_INFO_PATH = "/info"

# HTTP timeout for bridge calls (seconds). Matches the screen_time engine.
_HTTP_TIMEOUT_S = 15


def _bridge_url() -> str:
    """Resolve the bridge base URL (env override → loopback default)."""
    return os.environ.get(_BRIDGE_URL_ENV, _DEFAULT_BRIDGE_URL)


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
        return _BRIDGE_TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


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


def _fetch_bridge_json(path: str) -> dict[str, Any] | None:
    """GET a bridge endpoint with the bearer token; ``None`` on any failure.

    Fail-soft by design (mirrors the screen_time engine): a non-200, non-JSON,
    non-dict body, missing token, or transport error all return ``None`` so a
    caller treats an unreachable bridge as "no data", never a crash. This is the
    seam tests monkeypatch so no socket is opened.
    """
    import httpx

    token = _read_bridge_token()
    if not token:
        return None
    try:
        resp = httpx.get(
            f"{_bridge_url()}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def _post_bridge_json(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """POST a JSON body to a bridge endpoint; ``None`` on any failure.

    The mutating counterpart to :func:`_fetch_bridge_json`, used by ``set`` and
    the policy ``rollback`` restore. Same fail-soft contract: a non-2xx, missing
    token, or transport error returns ``None`` so the caller reports
    ``ok=False`` rather than raising mid-mutation. Tests monkeypatch this so no
    write ever reaches live gear.
    """
    import httpx

    token = _read_bridge_token()
    if not token:
        return None
    try:
        resp = httpx.post(
            f"{_bridge_url()}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=_HTTP_TIMEOUT_S,
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
        """Read one bridge endpoint by path; ``None`` when the bridge has no body.

        Returns the JSON payload serialized to a compact string (the uniform
        ``str | None`` the Protocol mandates), or ``None`` for an unreachable /
        empty endpoint — a normal best-effort outcome, not an error.
        """
        self._require_connected()
        data = _fetch_bridge_json(path)
        if data is None:
            return None
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    def set(self, path: str, value: str) -> OpResult:
        """Write one value to a bridge endpoint, returning before/after.

        The before is a best-effort read of the same path (so the audit log
        carries the prior state); the write is a bridge ``POST`` with a
        ``{"value": ...}`` body. A bridge that refuses (``None`` from the POST)
        yields ``ok=False`` rather than raising — the rails treat that as a
        failed apply and trip rollback. This op is composed behind
        ``guarded_apply`` at the intent layer; it is never auto-fired.
        """
        self._require_connected()
        before_data = _fetch_bridge_json(path)
        before = (
            json.dumps(before_data, separators=(",", ":"), ensure_ascii=False)
            if before_data is not None
            else None
        )
        result = _post_bridge_json(path, {"value": value})
        if result is None:
            return OpResult(
                ok=False,
                detail=f"bridge refused set {path}",
                before=before,
                after=None,
            )
        return OpResult(ok=True, detail=f"set {path}", before=before, after=value)

    def capabilities(self) -> AbstractSet[Capability]:
        """Operations this box actually supports through the bridge."""
        return {
            Capability.READ,
            Capability.POLICY,
            Capability.SCREEN_TIME,
            Capability.WAN_MODE,
        }

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
        """Restore the captured policy state through the bridge restore endpoint.

        Reports ``ok=False`` when the snapshot carries no restorable baseline
        (``snap.data`` empty, or no ``/policies`` key) — an empty rollback is NOT
        a success: it would leave a half-applied box (e.g. a policy still paused)
        while falsely reporting it was restored. The rails treat ``ok=False`` as
        a failed restore and surface a manual-recovery instruction.
        """
        captured = snap.data.get(_POLICIES_PATH)
        if not captured:
            return OpResult(
                ok=False,
                detail="rollback failed: snapshot carried no restorable policy baseline",
            )
        result = _post_bridge_json(_POLICIES_RESTORE_PATH, {"policies": captured})
        if result is None:
            return OpResult(
                ok=False,
                detail="rollback failed: bridge refused policy restore",
            )
        return OpResult(ok=True, detail="restored policy state")


# Self-register on import so the registry resolves ``firewalla`` to this provider.
registry.register(FirewallaProvider)
