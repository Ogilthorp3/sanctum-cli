"""Sagemcom F@st hub provider — the first real Layer-1 brand.

Bell Canada ships a Sagemcom F@st gateway (the "Home Hub") whose configuration
is driven over the SAH JSON-req transport with SHA-512 challenge auth. This
provider hides that transport behind the uniform :class:`DeviceProvider`
contract: ``connect`` logs in with a password read from the Keychain at call
time, ``get``/``set`` address single leaf values by XPath, and
``snapshot``/``rollback`` capture and restore the Bell-specific network-config
subtree so a Layer-2 intent (e.g. single-NAT) can be undone if verification
fails.

Every ``sagemcom_api`` client method is a coroutine; the provider owns one
small ``_run`` helper that drives a coroutine to completion. Crucially it does
*not* use :func:`asyncio.run` per call: the real ``SagemcomClient`` builds a
single long-lived ``aiohttp.ClientSession`` (with a ``TCPConnector``) in its
``__init__`` and reuses it via ``async with self.session.post(...)`` on every
request. aiohttp binds that session/connector to the event loop it is first
driven in; a fresh per-call ``asyncio.run`` loop would leave the session bound
to the (now-closed) login loop and the first op after ``connect()`` would raise
``RuntimeError: Event loop is closed``. So the provider owns ONE persistent loop
for its whole connected lifetime — created in :meth:`connect`, reused for every
get/set/snapshot, and torn down in :meth:`disconnect` (after a best-effort
``logout``/``close`` on the same loop so the aiohttp session is released cleanly).

The ``sagemcom_api`` import is deliberately lazy (inside :func:`_make_client`)
so importing this module never requires the optional transport dependency, and
so tests can mock ``_make_client`` and never touch the network at all.

Read paths are safe to exercise against the live hub (Task 7's env-gated
smoke); write paths are unit-tested against a mocked client and are NEVER fired
against live gear in the overnight build — the single-NAT cutover is
attended-only and out of scope here.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

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
from sanctum_cli.devices.transport import TransportKind

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from collections.abc import Set as AbstractSet

T = TypeVar("T")

# The admin password lives in the login Keychain under this (account, service)
# tuple — never on disk, read fresh on every connect.
KEYCHAIN_ACCOUNT = "admin"
KEYCHAIN_SERVICE = "bell-hub-admin"

# The Bell-specific network-config subtree we snapshot before a mutating intent.
# These are the leaf XPaths a single-NAT / DMZ cutover touches.
_BRIDGE_MODE_XPATH = "Device/Services/BellNetworkCfg/SetBridgeMode"
# VERIFIED against the live F5697 (2026-06-26): the AdvancedDMZ parent is a NESTED
# struct ({advanced_dmz:{enable, status, advanced_dm_zhost}}); the settable boolean
# leaf is .../AdvancedDMZ/Enable ('true'/'false' string), and .../Status is a
# read-only mirror. The engage write is Enable='true' (06-14 used Enable=false to
# disable). The MAC host is already paired to the Firewalla WAN MAC, so engaging
# is just this flag. Targeting the parent path with 'on' (the old guess) misfires.
_ADVANCED_DMZ_XPATH = "Device/Services/BellNetworkCfg/AdvancedDMZ/Enable"
_SNAPSHOT_XPATHS = (_BRIDGE_MODE_XPATH, _ADVANCED_DMZ_XPATH)

# The WiFi/guest/channel leaves the near-total setValue surface also reaches.
# These are the standard TR-181 Device:2 datamodel addresses (the BBF schema the
# SAH datamodel implements — a different-author source than this module),
# translated to SAH ``/`` xpath form. capability_op supplies the path a Layer-2
# intent sets; ``discover()`` is the per-hub verifier that confirms the specific
# leaf is settable (not pinned NON_WRITABLE / ACCESS_RESTRICTION) before a mutate.
_WIFI_ENABLE_XPATH = "Device/WiFi/SSID/1/Enable"
_GUEST_WIFI_ENABLE_XPATH = "Device/WiFi/SSID/2/Enable"
_CHANNEL_XPATH = "Device/WiFi/Radio/1/Channel"

# WAN_MODE on a Bell hub IS the bridge-vs-router control — the single-NAT cutover
# the flip drives. It shares the SetBridgeMode leaf with BRIDGE_MODE: two
# capability NAMES for the one physical WAN-operating-mode control on this hub, so
# both honestly resolve to the same real settable leaf.
_WAN_MODE_XPATH = _BRIDGE_MODE_XPATH

# The brand-owned vocabulary: high-level Capability → the Bell (path,
# engaged-value) that achieves it. A Layer-2 intent reaches each capability
# through this map (via capability_op), so it never hardcodes a Bell XPath —
# adding a non-TR-069 brand is one new provider with its own map, no intent
# change. Every feature-cap the hub advertises in :meth:`capabilities` is backed
# by an entry here (honest-verify: no advertised toggle-cap without a real op).
_CAPABILITY_OPS: dict[Capability, CapabilityOp] = {
    Capability.BRIDGE_MODE: CapabilityOp(path=_BRIDGE_MODE_XPATH, engaged="on"),
    # DMZ engage = AdvancedDMZ/Enable='true' (verified live: the leaf reads
    # 'true'/'false', NOT 'on'/'off'). Disengaged baseline is 'false' (see
    # _SAFE_BASELINES) so rollback drives the right value.
    Capability.DMZ: CapabilityOp(path=_ADVANCED_DMZ_XPATH, engaged="true"),
    Capability.WIFI: CapabilityOp(path=_WIFI_ENABLE_XPATH, engaged="true"),
    Capability.GUEST_WIFI: CapabilityOp(path=_GUEST_WIFI_ENABLE_XPATH, engaged="true"),
    Capability.CHANNELS: CapabilityOp(path=_CHANNEL_XPATH, engaged="auto"),
    Capability.WAN_MODE: CapabilityOp(path=_WAN_MODE_XPATH, engaged="on"),
}

# The leaves a single-NAT cutover actually MUTATES (derived from the capability
# ops so the two never drift). The snapshot MUST carry a restorable baseline for
# each even if the read returns None at snapshot time — otherwise a rollback after
# a failed cutover would have nothing to restore and would silently "succeed"
# while leaving the household in bridge mode / Advanced DMZ (internet down).
#
# BOTH the bridge-mode AND the Advanced-DMZ leaf are listed: the `single_nat`
# intent flips bridge mode, the `single_nat_dmz` orchestrator engages Advanced DMZ
# — both are single-NAT cutovers that strand the household if a rollback cannot
# disable them. The real Bell firmware returns None for an un-engaged leaf, so
# without DMZ here a failed DMZ cutover's rollback would have no DMZ baseline to
# restore and would leave the hub stuck in single-NAT (FIX-2, CRITICAL). A None
# read of either leaf means the hub is not in that mode, so the safe pre-cutover
# baseline is "off".
_MUTATED_XPATHS = (
    _CAPABILITY_OPS[Capability.BRIDGE_MODE].path,
    _CAPABILITY_OPS[Capability.DMZ].path,
)
# Per-leaf disengaged baseline — the value that means "NOT in this mode", used to
# guarantee a restorable rollback baseline when a leaf reads None at snapshot.
# These DIFFER per leaf (verified live): SetBridgeMode is 'off'; AdvancedDMZ/Enable
# is 'false'. A single shared "off" would restore the DMZ leaf to a value the
# firmware does not accept. Keyed by the mutated path.
_SAFE_BASELINES: dict[str, str] = {
    _CAPABILITY_OPS[Capability.BRIDGE_MODE].path: "off",
    _CAPABILITY_OPS[Capability.DMZ].path: "false",
}

# The honest capability map: each cap the hub advertises → (transport, concrete op).
# The feature-cap rows are DERIVED from ``_CAPABILITY_OPS`` so a leaf and its map
# entry can never drift; the verb rows (READ/SET/FIRMWARE/REBOOT) name the SAH verb
# that backs them. ``build_capability_map`` raises if ``capabilities()`` ever
# advertises a cap missing here, so an advertised-but-unbound power fails loudly.
_CAP_BINDINGS: dict[Capability, tuple[str, str]] = {
    Capability.READ: ("sah:getValue", "getValue <any leaf> (e.g. Device/DeviceInfo/*)"),
    Capability.SET: ("sah:setValue", "setValue <any settable leaf> (near-total SAH surface)"),
    Capability.FIRMWARE: (
        "sah:getValue",
        "getValue Device/DeviceInfo/SoftwareVersion (read-only — firmware image is NON_WRITABLE)",
    ),
    Capability.REBOOT: ("sah:reboot", "reboot (SAH reboot action, xpath=Device)"),
    **{
        cap: ("sah:setValue", f"setValue {op.path}={op.engaged}")
        for cap, op in _CAPABILITY_OPS.items()
    },
}

# The GUI/carrier ceiling: the two writability walls the audit found (firmware
# NON_WRITABLE + Bell ACCESS_RESTRICTION leaves), named so a caller is TOLD the
# ceiling rather than discovering it by a rejected setValue.
_GUI_ONLY_CEILING: tuple[str, ...] = (
    "firmware image (NON_WRITABLE — vendor/carrier-locked, no setValue)",
    "Bell carrier-locked network-config leaves (ACCESS_RESTRICTION)",
)

# XPath that identifies the device class once authenticated, used to refine the
# generic ``brand`` into a concrete model string after connect.
_PRODUCT_CLASS_XPATH = "Device/DeviceInfo/ProductClass"

# SAH reply ``error.description`` tokens that mean "succeeded". The ``sagemcom_api``
# transport's own ``__post`` treats only ``XMO_REQUEST_NO_ERR``/``"Ok"`` as
# top-level success; real successful setValue replies (and the test fakes that
# mirror an observed hub reply) carry ``XMO_NO_ERR``. We accept all three and
# fail-closed on everything else, because the transport RETURNS (does not raise)
# on an error description it does not model — so "the call did not raise" is NOT
# proof the write landed.
_SAH_OK = frozenset({"XMO_REQUEST_NO_ERR", "XMO_NO_ERR", "Ok"})

# Reboot-ONLY success tokens — the reboot verb's wider success vocabulary (FIX-1).
# A SAH reboot tears down its own session as the box restarts, so the request that
# issued it rarely gets a clean XMO_NO_ERR: the hub reports the action callback
# died with the session (XMO_ACTION_CALLBACK_ERR) or that it is already rebooting
# (XMO_REBOOTING_ERR). Both mean the reboot was INITIATED = SUCCESS. They are
# accepted ONLY for the reboot verb (never for set/action) — a setValue returning
# one of these is still a real failure. On 2026-06-26 reading these as failure
# tripped a rollback against an already-rebooting hub → DMZ left engaged un-armored.
_SAH_REBOOT_INITIATED = frozenset({"XMO_ACTION_CALLBACK_ERR", "XMO_REBOOTING_ERR"})
_SAH_REBOOT_OK = _SAH_OK | _SAH_REBOOT_INITIATED


def _reply_error(reply: Any, ok_tokens: frozenset[str] = _SAH_OK) -> str | None:
    """Return the first non-``ok_tokens`` SAH error description in a reply, else ``None``.

    Fail-closed: a reply is clean only when the top-level ``error.description`` is
    an accepted success token AND every action's ``error.description`` is too. A
    missing/garbled envelope, an unmodeled top-level error, or a failed action
    each yield a non-None description so the caller treats the write as failed —
    closing the transport's two swallow surfaces: it RETURNS (does not raise) on
    an unmodeled top-level error, and it never inspects per-action errors unless
    the top level is ``XMO_REQUEST_ACTION_ERR``.

    ``ok_tokens`` defaults to :data:`_SAH_OK` (the set/action success set); the
    reboot verb passes :data:`_SAH_REBOOT_OK` so the reboot-initiated tokens
    (XMO_ACTION_CALLBACK_ERR / XMO_REBOOTING_ERR) are treated as success for that
    verb ONLY — see :meth:`SagemcomHubProvider.reboot`.
    """
    if not isinstance(reply, dict):
        return f"unrecognized reply (not a dict: {type(reply).__name__})"
    inner = reply.get("reply")
    if not isinstance(inner, dict):
        return "reply missing 'reply' envelope"
    top = inner.get("error")
    top_desc = top.get("description") if isinstance(top, dict) else None
    if top_desc is None:
        return "reply missing top-level error.description"
    if top_desc not in ok_tokens:
        return str(top_desc)
    actions = inner.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            err = action.get("error")
            desc = err.get("description") if isinstance(err, dict) else None
            if desc is not None and desc not in ok_tokens:
                return str(desc)
    return None


# Exception types that mean the hub dropped its OWN connection as it began to
# reboot — the EXPECTED success signal for the reboot verb (FIX-1). ConnectionError
# (incl. ConnectionResetError), TimeoutError (asyncio.TimeoutError IS TimeoutError
# on 3.11+), and OSError cover the stdlib + aiohttp families (aiohttp's
# ServerDisconnectedError / ClientConnectionError / ServerTimeoutError are all
# OSError subclasses). Matched by base class so the optional aiohttp dep need not
# be imported here. A genuine rejection (DeviceError, auth) is NOT in this family,
# so it still fails closed.
_REBOOT_CONNECTION_DROP = (ConnectionError, TimeoutError, OSError)


def _is_reboot_connection_drop(exc: BaseException) -> bool:
    """True iff ``exc`` looks like the hub dropping its connection to reboot.

    Belt-and-suspenders for transports whose disconnect error is NOT an OSError
    subclass: match the known aiohttp/asyncio connection-drop class names too, so a
    ``ServerDisconnectedError`` (were it ever not an OSError) is still read as the
    reboot's success signal rather than a failure.
    """
    if isinstance(exc, _REBOOT_CONNECTION_DROP):
        return True
    return type(exc).__name__ in {
        "ServerDisconnectedError",
        "ClientConnectionError",
        "ClientOSError",
        "ConnectionResetError",
        "ServerTimeoutError",
        "TimeoutError",
    }


def _make_client(creds: Creds) -> Any:
    """Build a logged-out ``SagemcomClient`` for ``creds``.

    The ``sagemcom_api`` import is local so this module imports without the
    optional dependency present, and so tests monkeypatch this factory to inject
    a fake client (no network, no real auth). ``creds.secret`` must already hold
    the password (``connect`` reads it from the Keychain before calling here).
    """
    try:
        from sagemcom_api.client import SagemcomClient
        from sagemcom_api.enums import EncryptionMethod
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        msg = "sagemcom_api is required to talk to a Sagemcom hub"
        raise DeviceError(
            msg,
            fix="install the transport: pip install sagemcom_api",
        ) from exc

    return SagemcomClient(
        creds.host,
        creds.username,
        creds.secret or "",
        EncryptionMethod.SHA512,
        ssl=False,
    )


# The SAH action that reboots a Sagemcom F@st gateway — the exact dict the
# installed ``sagemcom_api`` client's own ``reboot()`` builds. Issued through the
# client's raw request path (see :func:`_reboot_raw`) so the provider receives the
# full reply envelope and can fail-closed via :func:`_reply_error`.
_REBOOT_ACTION = {
    "id": 0,
    "method": "reboot",
    "xpath": "Device",
    "parameters": {"source": "GUI"},
}


async def _reboot_raw(client: Any) -> Any:
    """Issue the SAH reboot action and return the RAW reply envelope.

    The convenience ``client.reboot()`` returns ``__get_response_value(response)``
    (the extracted leaf value), discarding the ``{"reply": {"error": ...}}``
    envelope the fail-closed check needs — and for the reboot action it yields the
    same ``''`` for a clean and a rejected reply, so success cannot be told from
    failure. The client's raw request method DOES return the full envelope, so we
    issue the action through it instead. Its name is mangled
    (``_SagemcomClient__api_request_async``); we reach it via ``getattr`` and fall
    back to the public ``reboot()`` coroutine only if a future library version
    drops/renames the raw method, so the provider still functions (with the
    library's own error handling) rather than breaking outright.
    """
    raw = getattr(client, "_SagemcomClient__api_request_async", None)
    if raw is not None:
        return await raw([dict(_REBOOT_ACTION)], False)
    # Defensive fallback: a client without the raw seam (e.g. a future rename).
    # The library's own ``reboot()`` raises on the errors it models; the swallowed
    # unmodeled-top-level surface is then out of our reach but no worse than the
    # library's baseline.
    return await client.reboot()


# The SAH datamodel verbs that mutate a multi-instance *table* (port-forwards,
# DHCP static leases, firewall rules) — the one config class a single-leaf
# ``setValue`` cannot reach — plus the generic escape hatch onto the full verb
# set the raw transport exposes. The installed ``sagemcom_api`` 1.4.3 ships NO
# convenience wrapper for any of these (only get/set/reboot), so they are issued
# through the SAME name-mangled raw request seam ``reboot()`` uses — which returns
# the full ``{"reply": {"error": ...}}`` envelope the fail-closed inspector reads.
# The xpath in get/set rides through the library's own ``urllib.parse.quote``;
# the raw seam does NOT quote, so the conventional getValue safe set is reused
# here (preserves the datamodel ``/=[]'`` syntax — incl. ``Table[index]`` — while
# encoding genuinely hostile chars exactly once).
_SAH_XPATH_SAFE = "/=[]'"


async def _action_raw(client: Any, action: dict[str, Any]) -> Any:
    """Issue ONE arbitrary SAH action through the raw request seam.

    Mirrors :func:`_reboot_raw` for the generic verb surface (action/add_row/
    delete_row/apply_changes). Crucially, unlike ``reboot`` — which the library
    models with a convenience ``client.reboot()`` — addChild/deleteChild/
    applyChanges have NO library wrapper, so the raw seam is the ONLY path that
    returns the fail-closed envelope. If a future ``sagemcom_api`` drops/renames
    the mangled method, there is no safe fallback that yields the envelope, so we
    raise :class:`DeviceError` rather than silently degrade to a green-looking
    no-op (the version guard in the boundary suite fails loudly on the same rename).
    """
    raw = getattr(client, "_SagemcomClient__api_request_async", None)
    if raw is None:
        msg = "sagemcom_api raw request seam (_SagemcomClient__api_request_async) is missing"
        raise DeviceError(
            msg,
            fix=(
                "the installed sagemcom_api dropped/renamed the raw seam; pin a "
                "version that exposes it (table/transaction verbs have no fallback)"
            ),
        )
    return await raw([action], False)


# ── read-only subtree discovery ─────────────────────────────────────────────
#
# The two writability WALL classes the audit found on the Bell F5697: a leaf is
# settable through the near-total ``setValue`` surface UNLESS its SAH ``flags``
# metadata carries one of these. ``NON_WRITABLE`` is the firmware-locked class
# (e.g. SoftwareVersion); ``ACCESS_RESTRICTION`` is the Bell-locked class (the
# carrier pins certain network-config leaves). Matched case-insensitively against
# the flags token string so a ``"read_only NON_WRITABLE"`` style value still trips
# the wall. Everything else discover() reports as settable.
_WALL_TOKENS = ("NON_WRITABLE", "ACCESS_RESTRICTION")

# The SAH attribute-form getValue marks a parameter LEAF with a ``value`` key
# (its current value); a non-leaf datamodel OBJECT is a dict of child names → child
# nodes with no ``value`` of its own. This is the discriminator the walk uses to
# tell a leaf from an object to recurse into.
_LEAF_VALUE_KEY = "value"
_LEAF_FLAGS_KEY = "flags"


@dataclass(frozen=True)
class DiscoveredLeaf:
    """One datamodel leaf found by :meth:`SagemcomHubProvider.discover`.

    ``path`` is the full SAH xpath (PascalCase preserved — the walk runs over the
    RAW, un-decamelized getValue envelope), ``value`` is the leaf's current value
    stringified (``None`` when the firmware surfaced none), ``writable`` is True
    iff no wall flag pins it, and ``restriction`` carries the wall token
    (``NON_WRITABLE`` / ``ACCESS_RESTRICTION``) when ``writable`` is False, else
    ``None``. The settable subset is ``[leaf for leaf in leaves if leaf.writable]``
    — what a Layer-2 intent gates on before composing a real ``set``.
    """

    path: str
    value: str | None
    writable: bool
    restriction: str | None


def _leaf_writability(flags: Any) -> tuple[bool, str | None]:
    """Derive (writable, restriction) from a SAH leaf's ``flags`` metadata.

    ``flags`` may be a token string (``"read_only NON_WRITABLE"``) or a list of
    tokens; either is normalized to one upper-cased blob and matched against the
    two wall classes. A leaf with no wall flag is settable (the near-total
    ``setValue`` surface reaches it); a walled leaf reports the first wall token
    found so the caller can show *why* it is locked.
    """
    if isinstance(flags, str):
        text = flags
    elif isinstance(flags, (list, tuple)):
        text = " ".join(str(token) for token in flags)
    else:
        text = ""
    upper = text.upper()
    for token in _WALL_TOKENS:
        if token in upper:
            return False, token
    return True, None


def _extract_subtree(reply: Any) -> dict[str, Any] | None:
    """Pull the datamodel subtree out of a raw getValue reply envelope.

    Navigates ``reply["reply"]["actions"][0]["callbacks"][0]["parameters"]
    ["value"]`` — the SAME path the installed ``sagemcom_api``'s own
    ``__get_response`` / ``__get_response_value`` use (derived from the library
    source, a different author than this producer — Contracts at the Boundary). We
    read the RAW envelope (not the library's decamelizing wrapper) precisely so the
    datamodel keys stay PascalCase; decamelize would rewrite ``WiFi`` → ``wi_fi``
    and mangle every discovered path. Any missing/garbled level yields ``None`` (an
    empty discovery), never a crash.
    """
    if not isinstance(reply, dict):
        return None
    try:
        params = reply["reply"]["actions"][0]["callbacks"][0]["parameters"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(params, dict):
        return None
    value = params.get(_LEAF_VALUE_KEY, params)
    return value if isinstance(value, dict) else None


def _walk_subtree(node: dict[str, Any], prefix: str, depth: int) -> list[DiscoveredLeaf]:
    """Walk ``node`` up to ``depth`` object levels, collecting annotated leaves.

    A child carrying a ``value`` key is a parameter leaf (recorded with its
    writability); a child that is a plain object dict is recursed into while the
    ``depth`` budget allows (``depth`` counts object levels below ``prefix`` — at
    ``depth == 1`` only this level's leaves are taken and no deeper object is
    descended). Scalars/lists at a level are skipped (not attribute-form leaves).
    """
    leaves: list[DiscoveredLeaf] = []
    for key, child in node.items():
        path = f"{prefix}/{key}"
        if isinstance(child, dict) and _LEAF_VALUE_KEY in child:
            raw_value = child[_LEAF_VALUE_KEY]
            writable, restriction = _leaf_writability(child.get(_LEAF_FLAGS_KEY))
            leaves.append(
                DiscoveredLeaf(
                    path=path,
                    value=None if raw_value is None else str(raw_value),
                    writable=writable,
                    restriction=restriction,
                )
            )
        elif isinstance(child, dict) and depth > 1:
            leaves.extend(_walk_subtree(child, path, depth - 1))
    return leaves


async def _discover_raw(client: Any, path: str, depth: int) -> Any:
    """Issue ONE read-only ``getValue`` at ``path`` and return the RAW envelope.

    Mirrors :func:`_action_raw` (same name-mangled raw seam, same fail-closed
    missing-seam contract) but for the discovery read. It must go through the raw
    seam — NOT the library's ``get_value_by_xpath`` — because that wrapper
    decamelizes the value and would mangle every datamodel path; the raw envelope
    preserves the PascalCase keys the walk reports. ``depth`` is threaded into the
    getValue ``options`` as a request-side hint; the provider ALSO bounds the walk
    by ``depth`` client-side, so the bound holds even if the firmware ignores the
    option. The xpath is URL-quoted exactly once with the datamodel-aware safe set
    (the raw seam does not quote), matching :meth:`action`.
    """
    raw = getattr(client, "_SagemcomClient__api_request_async", None)
    if raw is None:
        msg = "sagemcom_api raw request seam (_SagemcomClient__api_request_async) is missing"
        raise DeviceError(
            msg,
            fix=(
                "the installed sagemcom_api dropped/renamed the raw seam; pin a "
                "version that exposes it (discovery has no decamelize-safe fallback)"
            ),
        )
    action = {
        "id": 0,
        "method": "getValue",
        "xpath": quote(path, _SAH_XPATH_SAFE),
        "options": {"depth": depth},
    }
    return await raw([action], False)


def _probe_is_sagemcom(gateway_ip: str) -> bool:  # noqa: ARG001 - probe is mocked in tests
    """Read-only fingerprint: does the gateway look like a Sagemcom hub?

    Real implementation would issue an *unauthenticated* JSON-req and match the
    ``XMO_INVALID_SESSION_ERR`` shape Sagemcom firmware returns (or read
    ``ProductClass`` if a session exists). It is a pure read — no mutation — and
    is the seam tests monkeypatch so ``detect`` never opens a socket. Conservative
    default: assume *not* Sagemcom unless the probe positively says so.
    """
    return False


class SagemcomHubProvider:
    """Layer-1 control surface for a Sagemcom F@st (Bell Home Hub) gateway."""

    kind = "hub"
    brand = "sagemcom"

    def __init__(self) -> None:
        self._client: Any = None
        # One persistent event loop drives the whole connected lifetime so the
        # client's loop-bound aiohttp.ClientSession stays valid across calls.
        self._loop: asyncio.AbstractEventLoop | None = None

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Drive one SAH coroutine to completion on the provider's loop.

        Reuses the single loop opened in :meth:`connect` so the client's
        loop-bound ``aiohttp.ClientSession`` is driven on the *same* loop it was
        first used in. Using a fresh ``asyncio.run`` loop per call would close
        the loop the session bound to and break the next op with
        ``RuntimeError: Event loop is closed``.
        """
        if self._loop is None:
            msg = "Sagemcom hub not connected; call connect() first"
            raise DeviceError(msg, fix="call provider.connect(creds) before reading/writing")
        return self._loop.run_until_complete(coro)

    @staticmethod
    def detect(net: NetContext) -> float:
        """Confidence this provider drives the gear at ``net`` (read-only probe).

        Returns ``1.0`` when a default gateway is present and the read-only
        fingerprint identifies Sagemcom firmware, else ``0.0``. No credentials
        and no mutation — safe to call during a registry scan.
        """
        if not net.gateway_ip:
            return 0.0
        return 1.0 if _probe_is_sagemcom(net.gateway_ip) else 0.0

    def connect(self, creds: Creds | None) -> None:
        """Open an authenticated SAH session.

        Reads the admin password from the Keychain (the ``creds.secret`` field is
        ignored on purpose — secrets come from the Keychain, never the caller),
        builds the client via :func:`_make_client`, and runs the async login.
        Refines ``brand`` to a concrete model when the device advertises one.
        """
        if creds is None:
            msg = "Sagemcom hub requires creds (host/username); got None"
            raise DeviceError(msg, fix="pass Creds(host=..., username='admin')")

        # Read the password under the RESOLVED (service, account) the CLI threaded
        # through Creds — NOT the brand constants — so a haus that overrides
        # devices.hub.keychain.{service,account} actually reads from its own entry.
        # The username is the resolved account; keychain_service is the resolved
        # service. Both fall back to this module's per-brand default when the
        # caller did not resolve them (a direct connect, e.g. in a test, or the
        # default haus path — which resolves to exactly these constants anyway, so
        # the default behavior is unchanged).
        account = creds.username or KEYCHAIN_ACCOUNT
        service = creds.keychain_service or KEYCHAIN_SERVICE
        # Headless-safe resolution: macOS Keychain (GUI tier) → SOPS device-creds
        # (age-key, headless) → fail-closed. NEVER 1Password/op (its TouchID prompt
        # would block a headless daemon). See :mod:`sanctum_cli.devices.creds`.
        password = creds_resolver.resolve_secret(account=account, service=service)
        authed = Creds(
            host=creds.host,
            username=creds.username,
            secret=password,
            key_path=creds.key_path,
        )
        # Open the persistent loop FIRST, then BUILD the client + login inside a
        # single coroutine RUN ON that loop. The real SagemcomClient constructs its
        # aiohttp ClientSession/TCPConnector in __init__, which calls
        # asyncio.get_running_loop() — so it MUST be built while a loop is running,
        # not before (building it outside a running loop raises
        # "RuntimeError: no running event loop"). Login + every later op then share
        # this one loop the session is bound to.
        loop = asyncio.new_event_loop()
        self._loop = loop

        async def _build_and_login() -> Any:
            client = _make_client(authed)  # built INSIDE the running loop (aiohttp-safe)
            await client.login()
            return client

        try:
            client = loop.run_until_complete(_build_and_login())
        except DeviceError:
            self._teardown_loop()
            raise
        except Exception as exc:  # normalize any transport error
            self._teardown_loop()
            msg = f"Sagemcom login failed: {exc}"
            raise DeviceError(msg, fix="check the admin password in the Keychain") from exc
        self._client = client
        self._refine_brand()

    def auth_ok(self) -> bool:
        """True iff the last :meth:`connect` opened a genuinely authenticated session.

        The explicit auth oracle a read-only auth-probe (onboard's pairing gate)
        calls. This provider is ALREADY fail-closed at connect — a rejected login
        re-raises :class:`DeviceError`, and ``self._client`` is set ONLY after a
        successful login — so ``_client is not None`` is exactly "we authenticated".
        Exposing it uniformly (alongside Orbi's :meth:`OrbiProvider.auth_ok`) lets
        the probe verify auth the SAME way for every brand, instead of relying on a
        connect-raises convention that is faithful for this brand but not for a
        best-effort one.
        """
        return self._client is not None

    def _refine_brand(self) -> None:
        """Best-effort: turn ``sagemcom`` into ``sagemcom-<model>`` post-connect."""
        try:
            product = self._raw_get(_PRODUCT_CLASS_XPATH)
        except DeviceError:
            return
        if product:
            self.brand = f"sagemcom-{str(product).lower()}"

    def _require_client(self) -> Any:
        if self._client is None:
            msg = "Sagemcom hub not connected; call connect() first"
            raise DeviceError(msg, fix="call provider.connect(creds) before reading/writing")
        return self._client

    def _teardown_loop(self) -> None:
        """Close the persistent loop and drop the reference (idempotent)."""
        loop = self._loop
        self._loop = None
        if loop is not None and not loop.is_closed():
            loop.close()

    def __del__(self) -> None:
        """Safety net: reclaim the loop if a caller forgot :meth:`disconnect`.

        A persistent loop left open is reported by ``BaseEventLoop.__del__`` as
        an unraisable ``ResourceWarning``. Closing it here (no coroutine driven —
        unsafe at interpreter shutdown) silences that and frees the loop's fd.
        ``disconnect`` remains the correct, explicit teardown; this only covers
        the forgotten path.
        """
        with contextlib.suppress(Exception):
            self._teardown_loop()

    def disconnect(self) -> None:
        """Close the SAH session and the persistent loop (idempotent).

        Best-effort logs out and closes the client's aiohttp session on the
        *same* loop it was driven on (so the connector unwinds cleanly), then
        tears the loop down. Safe to call when never connected, and safe to call
        twice. Errors during teardown are swallowed — there is nothing useful a
        caller can do, and the loop is closed regardless so no resource leaks.
        """
        client = self._client
        loop = self._loop
        self._client = None
        if client is not None and loop is not None and not loop.is_closed():
            for closer in ("logout", "close"):
                method = getattr(client, closer, None)
                if method is None:
                    continue
                # Teardown is best-effort: a logout/close failure must not stop
                # us from reclaiming the loop below.
                with contextlib.suppress(Exception):
                    loop.run_until_complete(method())
        self._teardown_loop()

    def _raw_get(self, path: str) -> str | None:
        client = self._require_client()
        try:
            value = self._run(client.get_value_by_xpath(path))
        except Exception as exc:  # normalize any transport error
            msg = f"Sagemcom getValue failed for {path!r}: {exc}"
            raise DeviceError(msg) from exc
        return None if value is None else str(value)

    def get(self, path: str) -> str | None:
        """Read one leaf value by XPath; ``None`` when the path is unknown."""
        return self._raw_get(path)

    def set(self, path: str, value: str) -> OpResult:
        """Write one leaf value by XPath, returning before/after for the audit log.

        Fail-closed on a rejected write: the transport RETURNS (does not raise)
        on an error description it does not model, so we inspect the reply body
        ourselves. A swallowed failure would otherwise report a green cutover and
        the rails' auto-rollback would never fire (Contracts at the Boundary).
        """
        client = self._require_client()
        before = self._raw_get(path)
        try:
            reply = self._run(client.set_value_by_xpath(path, value))
        except Exception as exc:  # normalize any transport error
            msg = f"Sagemcom setValue failed for {path!r}: {exc}"
            raise DeviceError(msg) from exc
        err = _reply_error(reply)
        if err is not None:
            msg = f"Sagemcom setValue for {path!r} was rejected by the hub: {err}"
            raise DeviceError(
                msg, fix="the hub did not accept the write; check the leaf/value and retry"
            )
        return OpResult(ok=True, detail=f"set {path}", before=before, after=value)

    def reboot(self) -> OpResult:
        """Reboot the hub via the SAH ``reboot`` action — reboot-initiated = SUCCESS.

        The installed ``sagemcom_api`` client models the reboot as the SAH action
        ``{"method": "reboot", "xpath": "Device", "parameters": {"source":
        "GUI"}}``. We drive it on the provider's persistent loop — the same loop
        login bound to, so the client's loop-bound ``aiohttp`` session stays valid —
        and then inspect the *raw reply envelope* ourselves (NOT the convenience
        ``client.reboot()``, whose lossy ``__get_response_value`` returns ``''`` for
        both a clean and a rejected reply — see :func:`_reboot_raw`).

        **FIX-1 — the reboot success contract.** Unlike :meth:`set`, a reboot kills
        its own connection as the box restarts, so the request that issued it rarely
        gets a clean ``XMO_NO_ERR``. Three outcomes ALL mean "reboot initiated" =
        SUCCESS, and the contract treats them as such (Contracts at the Boundary —
        authored from the device's REAL reply shapes, not the old XMO_NO_ERR-only
        assumption that stranded the haus on 2026-06-26):

        * the connection drops mid-request (reset / server-disconnect / read
          timeout) — caught here and reported ``ok=True`` (the reboot fired);
        * the reply carries ``XMO_ACTION_CALLBACK_ERR`` (the action callback died
          with the session) — accepted via :data:`_SAH_REBOOT_OK`;
        * the reply carries ``XMO_REBOOTING_ERR`` (the box is already rebooting) —
          also accepted via :data:`_SAH_REBOOT_OK`.

        Still fail-closed on a GENUINE rejection: a transport error that is NOT a
        connection-drop, or an ``error.description`` outside :data:`_SAH_REBOOT_OK`
        (e.g. ``XMO_ACCESS_RESTRICTION_ERR``, an auth failure), raises
        :class:`DeviceError` — a reboot the hub actually refused never reports green.
        This contract is shared by the rollback's latch-reboot
        (:class:`~sanctum_cli.devices.intents._DmzRollbackProvider`), which calls
        this same ``reboot()`` — so the unwind survives the normal reboot drop too.
        """
        client = self._require_client()
        try:
            reply = self._run(_reboot_raw(client))
        except Exception as exc:  # normalize any transport error
            # A dropped connection right after issuing the reboot is the NORMAL,
            # EXPECTED success signal (the hub tore down its own session to reboot),
            # NOT a failure — report ok=True. Only a non-drop transport error fails.
            if _is_reboot_connection_drop(exc):
                return OpResult(
                    ok=True,
                    detail="reboot issued (hub dropped the connection — reboot initiated)",
                )
            msg = f"Sagemcom reboot failed: {exc}"
            raise DeviceError(
                msg, fix="check the hub is reachable and the session is valid"
            ) from exc
        err = _reply_error(reply, ok_tokens=_SAH_REBOOT_OK)
        if err is not None:
            msg = f"Sagemcom reboot was rejected by the hub: {err}"
            raise DeviceError(
                msg, fix="the hub did not accept the reboot; check admin rights and retry"
            )
        return OpResult(ok=True, detail="reboot issued")

    def action(
        self, method: str, xpath: str, parameters: dict[str, Any] | None = None
    ) -> OpResult:
        """Issue an arbitrary SAH verb by name, fail-closed on a rejected reply.

        The generic escape hatch onto the full SAH verb set the raw transport seam
        exposes (getValue/setValue/addChild/deleteChild/applyChanges/…). :meth:`set`
        only reaches a single *leaf*; this reaches the table and transaction verbs
        ``set`` cannot. Exactly like :meth:`set` / :meth:`reboot` (Contracts at the
        Boundary), it inspects the RAW reply envelope itself and fail-closes — the
        transport RETURNS (does not raise) on an error description it does not model,
        so "the call did not raise" is NOT proof the verb landed.

        The provider OWNS the xpath encoding here: ``set``/``get`` ride the
        library's own ``urllib.parse.quote``, but this verb issues through the raw
        seam (which does not quote), so the xpath is URL-quoted exactly once with
        the datamodel-aware safe set (``/=[]'`` preserved — incl. ``Table[index]`` —
        truly hostile chars encoded once, never double-quoted). ``parameters`` ride
        verbatim in the JSON body (no URL-quoting — they are JSON-serialized).
        """
        client = self._require_client()
        act: dict[str, Any] = {
            "id": 0,
            "method": method,
            "xpath": quote(xpath, _SAH_XPATH_SAFE),
            "parameters": parameters if parameters is not None else {},
        }
        try:
            reply = self._run(_action_raw(client, act))
        except DeviceError:
            # A missing raw seam (no fallback) is already a fail-closed DeviceError —
            # propagate it unwrapped rather than masking it as a transport error.
            raise
        except Exception as exc:  # normalize any transport error
            msg = f"Sagemcom {method} failed for {xpath!r}: {exc}"
            raise DeviceError(
                msg, fix="check the hub is reachable and the session is valid"
            ) from exc
        err = _reply_error(reply)
        if err is not None:
            msg = f"Sagemcom {method} for {xpath!r} was rejected by the hub: {err}"
            raise DeviceError(
                msg,
                fix="the hub did not accept the operation; check the verb/xpath/params and retry",
            )
        return OpResult(ok=True, detail=f"{method} {xpath}")

    def add_row(self, xpath: str, params: dict[str, Any]) -> OpResult:
        """Add a row to a multi-instance table via the SAH ``addChild`` verb.

        For the one config class :meth:`set` cannot reach — a table of instances:
        port-forwards (``Device/NAT/PortMapping``), DHCP static leases, firewall
        rules. ``xpath`` is the table object; ``params`` are the new instance's
        field values. Fail-closed via :meth:`action` (a rejected addChild raises
        rather than reporting a phantom row). Pair with :meth:`apply_changes` when
        the firmware batches the transaction.
        """
        return self.action("addChild", xpath, params)

    def delete_row(self, xpath: str, index: int) -> OpResult:
        """Delete row ``index`` from a multi-instance table via ``deleteChild``.

        ``xpath`` is the table object and ``index`` identifies the instance to
        remove (carried in the action's ``parameters`` as ``{"index": index}``).
        Fail-closed via :meth:`action`. If a firmware variant instead addresses the
        instance in the path (``Table[index]``), reach it through :meth:`action`
        directly — this convenience encodes the conventional SAH ``deleteChild``
        shape.
        """
        return self.action("deleteChild", xpath, {"index": index})

    def apply_changes(self) -> OpResult:
        """Commit pending table/leaf changes via the SAH ``applyChanges`` verb.

        Issued at the ``Device`` root (the conventional global-apply target, as
        :meth:`reboot` targets ``Device``). Fail-closed via :meth:`action`. Some
        firmwares auto-apply each ``setValue``/``addChild`` and treat this as a
        no-op; others batch a transaction that only lands on ``applyChanges`` — so
        a table mutation that must be durable calls this after the row verbs.
        """
        return self.action("applyChanges", "Device")

    def discover(self, path: str = "Device", depth: int = 1) -> list[DiscoveredLeaf]:
        """Read-only subtree walk: every leaf under ``path`` + its writability.

        Issues a SINGLE SAH ``getValue`` at ``path`` through the raw seam (NEVER a
        ``setValue`` — discovery must not mutate), then walks the returned datamodel
        subtree up to ``depth`` object levels, reporting each leaf's current value
        and whether it is *settable* on this hub (per its ``flags`` metadata — the
        firmware ``NON_WRITABLE`` and Bell ``ACCESS_RESTRICTION`` walls are reported
        ``writable=False`` with their restriction token; everything else is settable
        through the near-total ``setValue`` surface).

        This is the honest capability-mapping engine: a Layer-2 caller discovers
        which leaves are actually writable on a given hub before composing a
        mutating ``set``, instead of trusting a hardcoded path that the carrier may
        have locked. The walk runs over the RAW (un-decamelized) envelope so the
        datamodel paths stay PascalCase — see :func:`_extract_subtree`. A subtree
        the firmware does not surface yields an empty list (best-effort), not a
        crash; a missing raw seam fails closed (no decamelize-safe fallback).
        """
        client = self._require_client()
        try:
            reply = self._run(_discover_raw(client, path, depth))
        except DeviceError:
            # A missing raw seam is already a fail-closed DeviceError — propagate
            # it unwrapped rather than masking it as a transport error.
            raise
        except Exception as exc:  # normalize any transport error
            msg = f"Sagemcom discover failed for {path!r}: {exc}"
            raise DeviceError(
                msg, fix="check the hub is reachable and the session is valid"
            ) from exc
        subtree = _extract_subtree(reply)
        if not subtree:
            return []
        return _walk_subtree(subtree, path, depth)

    def capabilities(self) -> AbstractSet[Capability]:
        """Operations this hub actually supports.

        The feature-caps come straight from :data:`_CAPABILITY_OPS` (BRIDGE_MODE,
        DMZ, WIFI, GUEST_WIFI, CHANNELS, WAN_MODE) so every advertised toggle-cap
        is backed by a real settable-leaf op — the two can never drift. The verb
        caps (READ/SET/FIRMWARE/REBOOT) are backed by the provider's own methods.
        """
        return set(_CAPABILITY_OPS) | {
            Capability.READ,
            Capability.SET,
            Capability.FIRMWARE,
            Capability.REBOOT,
        }

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        """The Bell-specific (path, engaged) binding for ``capability``, or None.

        This is the brand-owned vocabulary a Layer-2 intent reaches through, so
        the intent never hardcodes a Bell TR-069 XPath. A non-TR-069 hub maps the
        same capabilities to its own paths/values via its own ``capability_op``.
        """
        return _CAPABILITY_OPS.get(capability)

    def fallback_transport(self) -> TransportKind:
        """The GUI transport for this hub's carrier-locked ceiling: agent-browser.

        A Bell Home Hub exposes a web admin UI at the gateway, so the surfaces the
        near-total ``setValue`` cannot reach (the firmware NON_WRITABLE image, the
        Bell ACCESS_RESTRICTION leaves) are driven through the web UI — the
        agent-browser transport (the priority chain's first non-API rung) — not the
        mobile-app rung. The multi-transport router reads this via the optional
        :class:`~sanctum_cli.devices.transport.FallbackTransportProvider` protocol.
        """
        return TransportKind.BROWSER

    def capability_map(self) -> CapabilityMap:
        """Honest "what can I change on this hub": real SAH ops + the carrier ceiling.

        Every cap :meth:`capabilities` advertises is bound to the concrete SAH verb
        + leaf that backs it (the feature rows derived from :data:`_CAPABILITY_OPS`,
        so a map entry can never outlive its leaf). The ceiling names the firmware
        NON_WRITABLE image and the Bell ACCESS_RESTRICTION leaves — the surfaces the
        near-total ``setValue`` cannot reach — so a caller is told the wall instead
        of hitting it. ``build_capability_map`` enforces bindings ≡ ``capabilities()``.
        """
        return build_capability_map(
            brand=self.brand,
            capabilities=self.capabilities(),
            bindings=_CAP_BINDINGS,
            ceiling=_GUI_ONLY_CEILING,
        )

    def list_paths(self) -> list[CapabilityBinding]:
        """The flat list of REAL, writable/readable SAH bindings on this hub."""
        return list(self.capability_map().bindings)

    def snapshot(self, scope: str | None = None) -> Snapshot:  # noqa: ARG002 - whole-subtree
        """Capture the Bell network-config leaves we may need to restore.

        A best-effort read of each tracked XPath, with one hard guarantee: every
        leaf a mutating intent will actually change (``_MUTATED_XPATHS`` — both the
        bridge-mode and the Advanced-DMZ leaf) is ALWAYS present in the baseline,
        even if its read returns None. A leaf the firmware does not surface at
        snapshot time would otherwise be dropped, leaving rollback with nothing to
        restore — so it would silently "succeed" while the hub stayed in bridge
        mode / Advanced DMZ (internet down). A None read of either leaf means "not
        in that mode", so the safe pre-cutover baseline is ``"off"``. Any other
        tracked-but-not-mutated leaf stays best-effort: captured only when read.
        """
        data: dict[str, str] = {}
        for xpath in _SNAPSHOT_XPATHS:
            value = self._raw_get(xpath)
            if value is not None:
                data[xpath] = value
        # Guarantee a restorable baseline for every leaf the cutover will mutate,
        # using the per-leaf disengaged value (SetBridgeMode='off', AdvancedDMZ/
        # Enable='false') so a None read never restores a value the firmware rejects.
        for xpath in _MUTATED_XPATHS:
            data.setdefault(xpath, _SAFE_BASELINES[xpath])
        return Snapshot(
            brand=self.brand,
            taken_at=datetime.now(tz=UTC).isoformat(),
            data=data,
        )

    def rollback(self, snap: Snapshot) -> OpResult:
        """Restore every captured leaf by re-issuing its setValue.

        Reports ``ok=False`` when the snapshot carries no restorable baseline
        (``snap.data`` empty) OR when the hub REJECTS any restore write — an
        empty *or partial* rollback is NOT a success: it would leave a
        half-applied device (e.g. the hub stuck in bridge mode) while falsely
        reporting it was restored. The rails treat an ``ok=False`` rollback as a
        failed restore and surface a manual-recovery instruction.
        """
        if not snap.data:
            return OpResult(
                ok=False,
                detail="rollback failed: snapshot carried no restorable baseline (0 keys)",
            )
        restored = 0
        failures: list[str] = []
        for xpath, value in snap.data.items():
            try:
                self.set(xpath, value)
            except DeviceError as exc:
                failures.append(f"{xpath}: {exc}")
                continue
            restored += 1
        if failures:
            total = restored + len(failures)
            return OpResult(
                ok=False,
                detail=(
                    f"rollback INCOMPLETE: restored {restored}/{total} leaf(s); "
                    f"failed: {'; '.join(failures)}"
                ),
            )
        return OpResult(ok=True, detail=f"rolled back {restored} key(s)")


# Self-register on import so the registry resolves ``hub`` to this provider.
registry.register(SagemcomHubProvider)
