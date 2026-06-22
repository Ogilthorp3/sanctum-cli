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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from sanctum_cli import keychain
from sanctum_cli.devices import registry
from sanctum_cli.devices.base import (
    Capability,
    Creds,
    DeviceError,
    NetContext,
    OpResult,
    Snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from collections.abc import Set as AbstractSet

T = TypeVar("T")

# The admin password lives in the login Keychain under this (account, service)
# tuple — never on disk, read fresh on every connect.
KEYCHAIN_ACCOUNT = "admin"
KEYCHAIN_SERVICE = "bell-hub-admin"

# The Bell-specific network-config subtree we snapshot before a mutating intent.
# These are the leaf XPaths a single-NAT / DMZ cutover touches; snapshot reads
# each best-effort (a path the firmware does not expose is simply skipped), and
# rollback re-issues a setValue for every captured leaf.
_BRIDGE_MODE_XPATH = "Device/Services/BellNetworkCfg/SetBridgeMode"
_ADVANCED_DMZ_XPATH = "Device/Services/BellNetworkCfg/AdvancedDMZ"
_SNAPSHOT_XPATHS = (_BRIDGE_MODE_XPATH, _ADVANCED_DMZ_XPATH)

# XPath that identifies the device class once authenticated, used to refine the
# generic ``brand`` into a concrete model string after connect.
_PRODUCT_CLASS_XPATH = "Device/DeviceInfo/ProductClass"


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

        password = keychain.read(account=KEYCHAIN_ACCOUNT, service=KEYCHAIN_SERVICE)
        authed = Creds(
            host=creds.host,
            username=creds.username,
            secret=password,
            key_path=creds.key_path,
        )
        client = _make_client(authed)
        # Open the persistent loop BEFORE login so login and every later op run
        # on the same loop the client's aiohttp session will bind to.
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(client.login())
        except DeviceError:
            self._teardown_loop()
            raise
        except Exception as exc:  # normalize any transport error
            self._teardown_loop()
            msg = f"Sagemcom login failed: {exc}"
            raise DeviceError(msg, fix="check the admin password in the Keychain") from exc
        self._client = client
        self._refine_brand()

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
        """Write one leaf value by XPath, returning before/after for the audit log."""
        client = self._require_client()
        before = self._raw_get(path)
        try:
            self._run(client.set_value_by_xpath(path, value))
        except Exception as exc:  # normalize any transport error
            msg = f"Sagemcom setValue failed for {path!r}: {exc}"
            raise DeviceError(msg) from exc
        return OpResult(ok=True, detail=f"set {path}", before=before, after=value)

    def capabilities(self) -> AbstractSet[Capability]:
        """Operations this hub actually supports."""
        return {
            Capability.READ,
            Capability.SET,
            Capability.BRIDGE_MODE,
            Capability.DMZ,
            Capability.WAN_MODE,
            Capability.FIRMWARE,
            Capability.REBOOT,
        }

    def snapshot(self, scope: str | None = None) -> Snapshot:  # noqa: ARG002 - whole-subtree
        """Capture the Bell network-config leaves we may need to restore.

        Reads each tracked XPath best-effort; paths the firmware does not expose
        are skipped (they cannot be rolled back to a value we never read).
        """
        data: dict[str, str] = {}
        for xpath in _SNAPSHOT_XPATHS:
            value = self._raw_get(xpath)
            if value is not None:
                data[xpath] = value
        return Snapshot(
            brand=self.brand,
            taken_at=datetime.now(tz=UTC).isoformat(),
            data=data,
        )

    def rollback(self, snap: Snapshot) -> OpResult:
        """Restore every captured leaf by re-issuing its setValue."""
        restored = 0
        for xpath, value in snap.data.items():
            self.set(xpath, value)
            restored += 1
        return OpResult(ok=True, detail=f"rolled back {restored} key(s)")


# Self-register on import so the registry resolves ``hub`` to this provider.
registry.register(SagemcomHubProvider)
