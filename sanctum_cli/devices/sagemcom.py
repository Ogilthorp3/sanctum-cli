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
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

import httpx

from sanctum_cli import keychain
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
    from collections.abc import Callable, Coroutine, Iterator
    from collections.abc import Set as AbstractSet

T = TypeVar("T")

# The admin password lives in the login Keychain under this (account, service)
# tuple — never on disk, read fresh on every connect.
KEYCHAIN_ACCOUNT = "admin"
KEYCHAIN_SERVICE = "bell-hub-admin"

# The Bell-specific network-config subtree we snapshot before a mutating intent.
# These are the leaf XPaths a single-NAT / DMZ cutover touches. The Advanced-DMZ
# leaf is the settable boolean ``.../AdvancedDMZ/Enable`` — verified live against
# the F5697 (2026-06-26): the AdvancedDMZ parent is a nested struct, and the
# boolean leaf accepts only the lowercase strings ``"true"``/``"false"`` (targeting
# the parent with ``"on"`` misfires with XMO_INVALID_PARAMETER_TYPE_ERR).
_BRIDGE_MODE_XPATH = "Device/Services/BellNetworkCfg/SetBridgeMode"
_ADVANCED_DMZ_XPATH = "Device/Services/BellNetworkCfg/AdvancedDMZ/Enable"
_SNAPSHOT_XPATHS = (_BRIDGE_MODE_XPATH, _ADVANCED_DMZ_XPATH)

# The brand-owned vocabulary: high-level Capability → the Bell TR-069 (path,
# engaged-value) that achieves it. A Layer-2 intent reaches bridge mode through
# this map (via capability_op), so it never hardcodes a Bell XPath — adding a
# non-TR-069 brand is one new provider with its own map, no intent change. The DMZ
# leaf is a boolean, so it engages with the SAH lowercase string ``"true"`` (it
# rejects ``"on"`` with XMO_INVALID_PARAMETER_TYPE_ERR).
_CAPABILITY_OPS: dict[Capability, CapabilityOp] = {
    Capability.BRIDGE_MODE: CapabilityOp(path=_BRIDGE_MODE_XPATH, engaged="on"),
    Capability.DMZ: CapabilityOp(path=_ADVANCED_DMZ_XPATH, engaged="true"),
}

# The leaves a single-NAT cutover actually MUTATES (derived from the capability ops
# so the two never drift). The snapshot MUST carry a restorable baseline for each
# even if the read returns None at snapshot time — otherwise a rollback after a
# failed cutover would have nothing to restore and would silently "succeed" while
# leaving the household in single-NAT (internet down). A None read means the leaf is
# not engaged, so the safe pre-cutover baseline is its DISENGAGED value: ``"off"``
# for the on/off bridge-mode leaf, ``"false"`` for the boolean Advanced-DMZ leaf
# (which is why the baseline is a per-leaf map, not one scalar — sending ``"off"``
# to the boolean DMZ leaf would be rejected).
_MUTATED_XPATHS = (
    _CAPABILITY_OPS[Capability.BRIDGE_MODE].path,
    _CAPABILITY_OPS[Capability.DMZ].path,
)
_SAFE_BASELINES: dict[str, str] = {
    _BRIDGE_MODE_XPATH: "off",
    _ADVANCED_DMZ_XPATH: "false",
}

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


def _sah_value(value: object) -> str:
    """Coerce a value to the SAH wire STRING the hub's ``setValue`` expects.

    The Bell SAH leaves are TYPED: a boolean leaf (e.g. ``AdvancedDMZ/Enable``)
    accepts ONLY the lowercase strings ``"true"``/``"false"`` and rejects anything
    else with ``XMO_INVALID_PARAMETER_TYPE_ERR`` (code 16777311) — the exact error
    that stranded the 2026-06-27 auto-rollback. The installed ``sagemcom_api``
    DESERIALIZES a boolean leaf to a Python ``bool``, so a snapshot taken via
    ``str(value)`` captured ``"True"``/``"False"`` (capitalized) and a restore of
    that baseline was rejected by the hub — DMZ stayed engaged, the household dark.
    Normalize the Python ``bool`` (and the capitalized repr an older ``str(bool)``
    snapshot may carry) to the SAH form so the ENGAGE's ``"true"`` and the
    ROLLBACK's ``"false"`` are the SAME string type. Any other value passes through
    as its ``str``.

    Owned at the SAH boundary (Contracts at the Boundary): every value crossing into
    ``set_value_by_xpath`` — a fresh write OR a restored baseline — and every value
    read back for a baseline is normalized here, so no caller can hand the hub a
    non-string boolean. ``isinstance(value, bool)`` is checked first because ``bool``
    is a subclass of ``int`` (``str(True)`` would otherwise give ``"True"``).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text in ("True", "False"):
        return text.lower()
    return text


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


def _iter_sah_error_descriptions(exc: BaseException) -> Iterator[str]:
    """Yield every SAH ``error.description`` string a raised exception carries.

    The installed ``sagemcom_api`` raises its typed errors as
    ``SomeException(action_error)`` where ``action_error`` is the SAH error dict
    (``{"description": "XMO_..."}``) — so the description we must classify rides in
    ``exc.args``. We walk the args (and one level of nested list/tuple) and surface
    any dict ``description`` plus any bare-string arg, so the reboot contract can
    read the token whether the library passed the dict or (defensively) a string.
    No token match happens here — the caller decides which descriptions mean what.
    """
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, Mapping):
            desc = arg.get("description")
            if isinstance(desc, str):
                yield desc
        elif isinstance(arg, str):
            yield arg
        elif isinstance(arg, (list, tuple)):
            for item in arg:
                if isinstance(item, Mapping):
                    nested = item.get("description")
                    if isinstance(nested, str):
                        yield nested


def _reboot_initiated_from_exc(exc: BaseException) -> bool:
    """True iff a RAISED transport exception carries a reboot-INITIATED SAH token.

    FIX-1 shape-B. The installed ``sagemcom_api`` does not only RETURN the
    reboot-initiated tokens; when ``XMO_ACTION_CALLBACK_ERR`` / ``XMO_REBOOTING_ERR``
    ride at the ACTION level under a top-level ``XMO_REQUEST_ACTION_ERR``, ``__post``
    RAISES ``UnknownException({"description": "XMO_ACTION_CALLBACK_ERR"})`` (verified
    against the installed client). That raise is the hub tearing down its session to
    reboot = SUCCESS, NOT a failure — so :meth:`SagemcomHubProvider.reboot` accepts
    it. A GENUINE typed rejection (``AccessRestrictionException`` /
    ``AuthenticationException``, whose description is NOT a reboot-initiated token)
    returns False and still fails closed. Mirrors :data:`_SAH_REBOOT_INITIATED` so
    the return-path and the raise-path accept EXACTLY the same tokens (Contracts at
    the Boundary — the two must never drift).
    """
    return any(
        desc in _SAH_REBOOT_INITIATED for desc in _iter_sah_error_descriptions(exc)
    )


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


# Read-only fingerprint constants. An unauthenticated SAH JSON-req to a Sagemcom
# hub is rejected with the ``XMO_INVALID_SESSION_ERR`` marker — a positive tell
# that costs one short-timeout POST and mutates nothing.
#
# NOTE (live-confirm — spec open item #2): the exact URL path (``/cgi/json-req``)
# and the marker are confirmed against Bert's live Bell F5697 during the attended
# smoke. The SHAPE (injected ``http_post`` seam + marker constant) is what this
# locks; a wrong constant is a one-line + fixture change, not a redesign.
_FINGERPRINT_TIMEOUT_S = 2.0
_SAGEMCOM_MARKER = "XMO_INVALID_SESSION_ERR"
_SAH_LOGIN_URL = "http://{ip}/cgi/json-req"
_SAH_PROBE_BODY = '{"request":{"id":0,"session-id":0,"method":"getValue","parameters":{}}}'


def _default_sah_post(url: str, data: str) -> str:
    """POST an unauthenticated SAH JSON-req; return the body (or "" on any error)."""
    try:
        resp = httpx.post(url, content=data, timeout=_FINGERPRINT_TIMEOUT_S)
    except (httpx.HTTPError, OSError):
        return ""
    return resp.text


def _probe_is_sagemcom(
    gateway_ip: str,
    *,
    http_post: Callable[[str, str], str] = _default_sah_post,
) -> bool:
    """Read-only fingerprint: does the gateway look like a Sagemcom hub?

    An unauthenticated SAH JSON-req to a Sagemcom hub returns the
    ``XMO_INVALID_SESSION_ERR`` shape. Pure read — no mutation, no auth. The
    ``http_post`` seam is injected so tests never open a socket; the default
    poster (httpx) is exercised only at the live boundary. Conservative default:
    assume *not* Sagemcom unless the marker is positively seen.
    """
    body = ""
    try:
        body = http_post(_SAH_LOGIN_URL.format(ip=gateway_ip), _SAH_PROBE_BODY)
    except Exception:  # a probe must never raise into detect()
        return False
    return _SAGEMCOM_MARKER in body


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
        password = keychain.read(account=account, service=service)
        authed = Creds(
            host=creds.host,
            username=creds.username,
            secret=password,
            key_path=creds.key_path,
        )
        # Open the persistent loop FIRST, then build the client AND login INSIDE it.
        # SagemcomClient.__init__ builds an aiohttp TCPConnector, which on modern
        # aiohttp calls asyncio.get_running_loop() at init — so the client MUST be
        # constructed while a loop is running. Building it before the loop (the old
        # order) raised "RuntimeError: no running event loop". Creating it inside
        # loop.run_until_complete binds its session to self._loop, the same loop
        # every later op is driven on via _run(), preserving the persistent-loop design.
        loop = asyncio.new_event_loop()
        self._loop = loop

        async def _build_and_login() -> Any:
            built = _make_client(authed)
            await built.login()
            return built

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
        # Coerce a boolean leaf's Python-bool read to the SAH wire string so a
        # captured baseline is never poisoned with "True"/"False" (which a later
        # restore would send back and the hub would reject) — see _sah_value.
        return None if value is None else _sah_value(value)

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
        # Normalize at the boundary: a Python bool or its capitalized repr (a typed
        # baseline / older str(bool) snapshot) becomes the SAH "true"/"false" the
        # boolean leaves require — otherwise the hub rejects it with
        # XMO_INVALID_PARAMETER_TYPE_ERR and an auto-rollback cannot disable DMZ.
        wire = _sah_value(value)
        try:
            reply = self._run(client.set_value_by_xpath(path, wire))
        except Exception as exc:  # normalize any transport error
            msg = f"Sagemcom setValue failed for {path!r}: {exc}"
            raise DeviceError(msg) from exc
        err = _reply_error(reply)
        if err is not None:
            msg = f"Sagemcom setValue for {path!r} was rejected by the hub: {err}"
            raise DeviceError(
                msg, fix="the hub did not accept the write; check the leaf/value and retry"
            )
        return OpResult(ok=True, detail=f"set {path}", before=before, after=wire)

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
            # FIX-1 shape-B: the installed sagemcom_api RAISES (not returns) when a
            # reboot-initiated token rides at the ACTION level under a top-level
            # XMO_REQUEST_ACTION_ERR (__post → UnknownException({"description":
            # "XMO_ACTION_CALLBACK_ERR"})). That raise is the reboot firing, NOT a
            # rejection, so accept it — otherwise a reboot that succeeded is read as a
            # failed stage and the rails roll back (the precise 06-26 cascade). A
            # genuine typed rejection (access-restriction/auth) carries no reboot
            # token and still falls through to the fail-closed raise below.
            if _reboot_initiated_from_exc(exc):
                return OpResult(
                    ok=True,
                    detail=(
                        "reboot issued (hub raised a reboot-initiated SAH token "
                        "— reboot initiated)"
                    ),
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

    def capabilities(self) -> AbstractSet[Capability]:
        """Operations this hub actually supports."""
        return set(_CAPABILITY_OPS) | {
            Capability.READ,
            Capability.SET,
            Capability.WAN_MODE,
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

    def snapshot(self, scope: str | None = None) -> Snapshot:  # noqa: ARG002 - whole-subtree
        """Capture the Bell network-config leaves we may need to restore.

        A best-effort read of each tracked XPath, with one hard guarantee: every
        leaf a mutating intent will actually change (``_MUTATED_XPATHS`` — bridge
        mode AND Advanced DMZ) is ALWAYS present in the baseline, even if its read
        returns None. A leaf the firmware does not surface at snapshot time (the real
        Bell shape: a getValue of an un-engaged Advanced-DMZ leaf returns None) would
        otherwise be dropped, leaving rollback with nothing to restore — so a failed
        cutover would silently "succeed" while the hub stayed in single-NAT (internet
        down). A None read means the leaf is not engaged, so the safe pre-cutover
        baseline is its DISENGAGED value from ``_SAFE_BASELINES`` (``"off"`` for the
        on/off bridge leaf, ``"false"`` for the boolean DMZ leaf).
        """
        data: dict[str, str] = {}
        for xpath in _SNAPSHOT_XPATHS:
            value = self._raw_get(xpath)
            if value is not None:
                data[xpath] = value
        # Guarantee a restorable baseline for every leaf the cutover will mutate.
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
