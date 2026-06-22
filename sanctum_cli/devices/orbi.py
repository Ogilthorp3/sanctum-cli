"""Orbi (NETGEAR) mesh-router provider — the mesh-AP brand on the contract.

A NETGEAR Orbi router/satellite system is driven through the maintained
``pynetgear`` SOAP transport, hidden behind the uniform
:class:`~sanctum_cli.devices.base.DeviceProvider` surface: ``connect`` logs in
with an admin password read from the Keychain at call time, ``get``/``set``
address guest-wifi and channel state by a small provider-owned path vocabulary,
and ``snapshot``/``rollback`` capture and restore that state so a Layer-2 intent
(e.g. toggling guest wifi) can be undone if verification fails.

The ``pynetgear`` import is deliberately LAZY (inside :func:`_make_client`) so
importing this module never requires the optional transport dependency, and so
tests can mock ``_make_client`` and never touch the network at all. ``pynetgear``
ships no SOAP-async surface — every method is a plain synchronous call — so this
provider owns no event loop (unlike the Sagemcom hub); it holds one logged-in
``Netgear`` client for its connected lifetime and drops it on ``disconnect``.

Read paths (guest-wifi state, channel, firmware, model) are safe to exercise
against a live Orbi (the env-gated ``SANCTUM_LIVE_ORBI=1`` smoke); write paths
(guest-wifi / channels) are unit-tested against a mocked client and are NEVER
fired against live gear in the overnight build — every mutation is composed
behind :func:`sanctum_cli.devices.rails.guarded_apply` (dry-run / guarded_apply
rails) at the intent layer and defaults to dry-run.

We have NO live Orbi creds yet, so live validation is deferred — ``connect`` is
tolerant of an unreachable / un-authable box (best-effort brand refine), exactly
like the other providers, and the build never blocks on a live call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
    from collections.abc import Set as AbstractSet

# The admin password lives in the login Keychain under this (account, service)
# tuple — never on disk, read fresh on every connect.
KEYCHAIN_ACCOUNT = "admin"
KEYCHAIN_SERVICE = "orbi-admin"

# Provider-owned path vocabulary: stable leaf addresses the intent layer / CLI
# use, mapped below to concrete pynetgear getters/setters. Keeping these brand-
# neutral-looking (``guest_wifi/<band>``, ``channel/<band>``) means a Layer-2
# intent never learns a pynetgear method name.
_GUEST_2G = "guest_wifi/2g"
_GUEST_5G = "guest_wifi/5g"
_CHANNEL_2G = "channel/2g"
_CHANNEL_5G = "channel/5g"
_INFO_MODEL = "info/model"
_FIRMWARE = "firmware/new"

# The leaves a guest-wifi / channel intent snapshots before mutating. A snapshot
# MUST carry a restorable baseline for each even if a read returns None at
# snapshot time — otherwise a rollback after a failed change would have nothing
# to restore and would silently "succeed" while leaving guest wifi engaged. For
# the guest-wifi leaves a None read means "could not read → assume off" (the safe
# baseline is "off"); for channels we only capture what we can read.
_GUEST_PATHS = (_GUEST_2G, _GUEST_5G)
_CHANNEL_PATHS = (_CHANNEL_2G, _CHANNEL_5G)
_SAFE_GUEST_BASELINE = "off"

# The brand-owned vocabulary: high-level Capability → the Orbi (path,
# engaged-value) that achieves it. A Layer-2 intent reaches guest-wifi through
# this map (via capability_op), so it never hardcodes an Orbi path — adding a
# brand is one new provider with its own map, no intent change. GUEST_WIFI binds
# to the 5 GHz band leaf (the band a "turn on the guest network" intent toggles).
_CAPABILITY_OPS: dict[Capability, CapabilityOp] = {
    Capability.GUEST_WIFI: CapabilityOp(path=_GUEST_5G, engaged="on"),
}


def _make_client(creds: Creds) -> Any:
    """Build a logged-out ``pynetgear.Netgear`` client for ``creds``.

    The ``pynetgear`` import is local so this module imports without the optional
    dependency present, and so tests monkeypatch this factory to inject a fake
    client (no network, no real auth). ``creds.secret`` must already hold the
    password (``connect`` reads it from the Keychain before calling here).
    """
    try:
        from pynetgear import Netgear
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        msg = "pynetgear is required to talk to a NETGEAR Orbi"
        raise DeviceError(
            msg,
            fix="install the transport: pip install pynetgear",
        ) from exc

    return Netgear(
        password=creds.secret or "",
        host=creds.host,
        user=creds.username,
    )


def _probe_is_orbi(gateway_ip: str) -> bool:  # noqa: ARG001 - probe is mocked in tests
    """Read-only fingerprint: does the gateway look like a NETGEAR Orbi?

    A real implementation would issue an *unauthenticated* HTTP/SOAP probe to the
    gateway and match the NETGEAR/Orbi model banner (the ``currentsetting.htm``
    ``Model=RBR...`` line, or the SOAP ``GetInfo`` device class). It is a pure
    read — no mutation, no auth — and is the seam tests monkeypatch so ``detect``
    never opens a socket. Conservative default: assume *not* Orbi unless the probe
    positively says so.
    """
    return False


class OrbiProvider:
    """Layer-1 control surface for a NETGEAR Orbi mesh router (pynetgear SOAP)."""

    kind = "orbi"
    brand = "orbi"

    def __init__(self) -> None:
        self._client: Any = None

    @staticmethod
    def detect(net: NetContext) -> float:
        """Confidence this provider drives the gear at ``net`` (read-only probe).

        Returns ``1.0`` when a default gateway is present and the read-only
        fingerprint identifies an Orbi/NETGEAR banner, else ``0.0``. No
        credentials and no mutation — safe to call during a registry scan.
        """
        if not net.gateway_ip:
            return 0.0
        return 1.0 if _probe_is_orbi(net.gateway_ip) else 0.0

    def connect(self, creds: Creds | None) -> None:
        """Open an authenticated pynetgear session.

        Reads the admin password from the Keychain (the ``creds.secret`` field is
        ignored on purpose — secrets come from the Keychain, never the caller),
        builds the client via :func:`_make_client`, and logs in. Login is
        BEST-EFFORT: an unreachable / un-authable box leaves ``brand`` as the
        generic ``orbi`` rather than failing the connect (reads degrade to a
        legible :class:`DeviceError`, never a crash at connect — we have no live
        creds yet, so the build must never block on a live call). Refines
        ``brand`` to a concrete model when login succeeds and the device
        advertises one.
        """
        if creds is None:
            msg = "Orbi requires creds (host/username); got None"
            raise DeviceError(msg, fix="pass Creds(host=..., username='admin')")

        # Read the password under the RESOLVED (service, account) the CLI threaded
        # through Creds — NOT the brand constants — so a haus that overrides
        # devices.orbi.keychain.{service,account} actually reads from its own entry.
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
        client = _make_client(authed)
        self._client = client
        # Login is best-effort: a failed/unreachable login must not raise at
        # connect (we have no live creds yet). The client is retained either way;
        # a later get/set raises DeviceError on the transport failure per contract.
        # ``pynetgear.login()`` returns a bool — False (or any falsey) means the
        # box rejected the creds, so we do NOT refine the brand off a session that
        # is not authed (a stale get_info read could otherwise mislead). Only a
        # truthy login refines ``brand`` to the concrete model.
        try:
            ok = client.login()
        except Exception:  # tolerate any login transport error (best-effort connect)
            return
        if ok:
            self._refine_brand()

    def _refine_brand(self) -> None:
        """Best-effort: turn ``orbi`` into ``orbi-<model>`` post-connect."""
        try:
            info = self._raw_get_info()
        except DeviceError:
            return
        model = (info or {}).get("ModelName")
        if model:
            self.brand = f"orbi-{str(model).lower()}"

    def _require_client(self) -> Any:
        if self._client is None:
            msg = "Orbi not connected; call connect() first"
            raise DeviceError(msg, fix="call provider.connect(creds) before reading/writing")
        return self._client

    def disconnect(self) -> None:
        """Release the pynetgear client. Idempotent + safe when never connected.

        ``pynetgear`` holds no long-lived socket (each SOAP call is a per-request
        HTTP POST), so teardown just drops the client reference. Safe to call when
        never connected and safe to call twice — the uniform lifecycle-close the
        Protocol mandates.
        """
        self._client = None

    def _raw_get_info(self) -> dict[str, Any] | None:
        client = self._require_client()
        try:
            info = client.get_info()
        except Exception as exc:  # normalize any transport error
            msg = f"Orbi get_info failed: {exc}"
            raise DeviceError(msg) from exc
        return info if isinstance(info, dict) else None

    def _get_guest(self, band: str) -> str | None:
        client = self._require_client()
        getter = getattr(client, f"get_{band}_guest_access_enabled")
        try:
            enabled = getter()
        except Exception as exc:  # normalize any transport error
            msg = f"Orbi guest-access read failed for {band}: {exc}"
            raise DeviceError(msg) from exc
        if enabled is None:
            return None
        return "on" if enabled else "off"

    def _get_channel(self, band: str) -> str | None:
        client = self._require_client()
        getter = getattr(client, f"get_{band}_info")
        try:
            info = getter()
        except Exception as exc:  # normalize any transport error
            msg = f"Orbi {band} info read failed: {exc}"
            raise DeviceError(msg) from exc
        if not isinstance(info, dict):
            return None
        channel = info.get("Channel")
        return None if channel is None else str(channel)

    def get(self, path: str) -> str | None:
        """Read one value by provider path; ``None`` when the path is unknown.

        Honors the :class:`~sanctum_cli.devices.base.DeviceProvider` Protocol
        contract: returns the value as a string, or ``None`` for a path this
        provider does not address (a normal best-effort outcome). A transport /
        auth failure raises :class:`DeviceError` rather than masquerading as an
        unknown path — matching Sagemcom's and Firewalla's ``get``.
        """
        if path == _GUEST_2G:
            return self._get_guest("2g")
        if path == _GUEST_5G:
            return self._get_guest("5g")
        if path == _CHANNEL_2G:
            return self._get_channel("2g")
        if path == _CHANNEL_5G:
            return self._get_channel("5g")
        if path == _INFO_MODEL:
            info = self._raw_get_info()
            model = (info or {}).get("ModelName")
            return None if model is None else str(model)
        if path == _FIRMWARE:
            client = self._require_client()
            try:
                fw = client.check_new_firmware()
            except Exception as exc:  # normalize any transport error
                msg = f"Orbi firmware check failed: {exc}"
                raise DeviceError(msg) from exc
            if not isinstance(fw, dict):
                return None
            new = fw.get("NewVersion")
            return None if new is None else str(new)
        # Unknown path → best-effort None (a path this provider does not expose),
        # NOT an error — but only after _require_client() so a pre-connect call
        # still fails legibly rather than silently returning None.
        self._require_client()
        return None

    def _set_guest(self, band: str, value: str) -> OpResult:
        client = self._require_client()
        path = _GUEST_2G if band == "2g" else _GUEST_5G
        before = self._get_guest(band)
        engaged = value == "on"
        setter = getattr(client, f"set_{band}_guest_access_enabled")
        try:
            setter(engaged)
        except Exception as exc:  # normalize any transport error
            msg = f"Orbi guest-access set failed for {band}: {exc}"
            raise DeviceError(msg) from exc
        return OpResult(ok=True, detail=f"set {path}", before=before, after=value)

    def set(self, path: str, value: str) -> OpResult:
        """Write one value by provider path, returning before/after for the audit log.

        Only guest-wifi leaves are writable today (the channel leaves are read-only
        on this surface); an unknown / unwritable path yields ``ok=False`` rather
        than raising, so the rails treat it as a failed apply (and trip rollback)
        instead of crashing mid-mutation. This op is composed behind
        ``guarded_apply`` at the intent layer; it is never auto-fired.
        """
        if path == _GUEST_2G:
            return self._set_guest("2g", value)
        if path == _GUEST_5G:
            return self._set_guest("5g", value)
        # Not a writable leaf on this surface.
        self._require_client()
        return OpResult(ok=False, detail=f"orbi: path not writable: {path}")

    def capabilities(self) -> AbstractSet[Capability]:
        """Operations this Orbi actually supports."""
        return {
            Capability.READ,
            Capability.FIRMWARE,
            Capability.AP_MODE,
            Capability.CHANNELS,
            Capability.GUEST_WIFI,
        }

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        """The Orbi-specific (path, engaged) binding for ``capability``, or None.

        This is the brand-owned vocabulary a Layer-2 intent reaches through, so
        the intent never hardcodes an Orbi path. ``GUEST_WIFI`` binds to the 5 GHz
        guest leaf; every other capability returns ``None`` (no blind mutation).
        """
        return _CAPABILITY_OPS.get(capability)

    def snapshot(self, scope: str | None = None) -> Snapshot:  # noqa: ARG002 - whole guest+channel
        """Capture guest-wifi + channel state we may need to restore.

        A best-effort read of each tracked leaf, with one hard guarantee: every
        guest-wifi leaf a mutating intent will actually change is ALWAYS present
        in the baseline, even if its read returns None. A leaf the firmware does
        not surface at snapshot time would otherwise be dropped, leaving rollback
        with nothing to restore — so it would silently "succeed" while guest wifi
        stayed engaged. For a guest leaf a None read means "could not read →
        assume off", so the safe baseline is ``"off"``. Channels stay best-effort
        (captured only when read), since the surface does not write them.
        """
        data: dict[str, str] = {}
        for path in _GUEST_PATHS:
            value = self.get(path)
            if value is not None:
                data[path] = value
        for path in _CHANNEL_PATHS:
            value = self.get(path)
            if value is not None:
                data[path] = value
        # Guarantee a restorable baseline for every guest leaf a change will mutate.
        for path in _GUEST_PATHS:
            data.setdefault(path, _SAFE_GUEST_BASELINE)
        return Snapshot(
            brand=self.brand,
            taken_at=datetime.now(tz=UTC).isoformat(),
            data=data,
        )

    def rollback(self, snap: Snapshot) -> OpResult:
        """Restore every captured guest-wifi leaf by re-issuing its setter.

        Reports ``ok=False`` when the snapshot carries no restorable baseline
        (``snap.data`` empty) — an empty rollback is NOT a success: it would
        leave a half-applied device (e.g. guest wifi still engaged) while falsely
        reporting it was restored. The rails treat an ``ok=False`` rollback as a
        failed restore and surface a manual-recovery instruction. Channel leaves
        are read-only here, so only the guest leaves are re-applied.
        """
        if not snap.data:
            return OpResult(
                ok=False,
                detail="rollback failed: snapshot carried no restorable baseline (0 keys)",
            )
        restored = 0
        for path, value in snap.data.items():
            if path in _GUEST_PATHS:
                self.set(path, value)
                restored += 1
        if restored == 0:
            return OpResult(
                ok=False,
                detail="rollback failed: snapshot carried no restorable guest-wifi leaf",
            )
        return OpResult(ok=True, detail=f"rolled back {restored} key(s)")


# Self-register on import so the registry resolves ``orbi`` to this provider.
registry.register(OrbiProvider)
