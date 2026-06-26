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

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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

# The device-access status tokens pynetgear's ``allow_block_device`` expects.
# Mirrors ``pynetgear.const.BLOCK`` / ``ALLOW`` (kept inline so importing this
# module never pulls the optional transport — the values are part of the stable
# SOAP contract; the boundary test re-derives them from the real library).
_STATUS_BLOCK = "Block"
_STATUS_ALLOW = "Allow"


@dataclass(frozen=True)
class OrbiAction:
    """One wired pynetgear write/action — Orbi's unit of discovery.

    ``pynetgear`` exposes a FIXED set of 49 SOAP methods with NO escape hatch and
    NO discovery, so the writable surface is enumerable up front: each
    :class:`OrbiAction` binds a stable, brand-neutral ``name`` (what the CLI /
    intent layer drives) to the concrete pynetgear ``method`` it issues, the
    high-level :class:`Capability` it backs, and its ``arg`` shape
    (``"none"`` / ``"value"`` boolean toggle / ``"mac"`` per-MAC allow-block).
    :meth:`OrbiProvider.list_actions` returns these — the brand's discovery.
    """

    name: str
    method: str
    capability: Capability
    arg: str = "none"


# The wired write/action surface: every pynetgear method that mutates the box
# (each rides ``Netgear._set`` → returns a bool we fail-close on). This is the
# SINGLE source of truth for both :meth:`OrbiProvider.action` dispatch and
# :meth:`OrbiProvider.capabilities` (a write-cap is advertised iff an action here
# backs it — they can never drift into a phantom cap).
_ACTIONS: dict[str, OrbiAction] = {
    action.name: action
    for action in (
        OrbiAction("reboot", "reboot", Capability.REBOOT),
        OrbiAction("update_firmware", "update_new_firmware", Capability.FIRMWARE),
        OrbiAction("set_qos", "set_qos_enable_status", Capability.FEATURE_TOGGLE, "value"),
        OrbiAction(
            "set_smart_connect", "set_smart_connect_enabled", Capability.FEATURE_TOGGLE, "value"
        ),
        OrbiAction("set_traffic_meter", "enable_traffic_meter", Capability.FEATURE_TOGGLE, "value"),
        OrbiAction(
            "set_block_device_enable", "set_block_device_enable", Capability.POLICY, "value"
        ),
        OrbiAction("allow_block_device", "allow_block_device", Capability.POLICY, "mac"),
        OrbiAction("speed_test", "set_speed_test_start", Capability.SPEEDTEST),
    )
}

def _orbi_cap_bindings() -> dict[Capability, tuple[str, str]]:
    """The honest cap → (transport, concrete pynetgear op) map for an Orbi.

    The write rows are DERIVED from :data:`_ACTIONS` (the single source of truth for
    the wired SOAP surface), so a cap is bound to exactly the real ``Netgear``
    method(s) that back it and the two can never drift; a cap backed by several
    actions (FEATURE_TOGGLE, POLICY) lists each method. READ and GUEST_WIFI are not
    in ``_ACTIONS`` (they are getters / the guest setter), so they are added
    explicitly. AP_MODE and CHANNELS are absent BY DESIGN — pynetgear ships no
    set-AP-mode / set-channel SOAP action and no escape hatch, so binding either
    would name an op that does not exist (they live in the GUI-only ceiling instead).
    """
    bindings: dict[Capability, tuple[str, str]] = {
        Capability.READ: (
            "pynetgear-soap",
            "get_attached_devices / get_satellites / get_*_info (read-only getters)",
        ),
        Capability.GUEST_WIFI: (
            "pynetgear-soap",
            "set_5g_guest_access_enabled / set_2g_guest_access_enabled",
        ),
    }
    by_cap: dict[Capability, list[str]] = {}
    for action in _ACTIONS.values():
        by_cap.setdefault(action.capability, []).append(action.method)
    for cap, methods in by_cap.items():
        bindings[cap] = ("pynetgear-soap", " / ".join(sorted(methods)))
    return bindings


_CAP_BINDINGS: dict[Capability, tuple[str, str]] = _orbi_cap_bindings()

# The GUI-only ceiling: the surfaces pynetgear's FIXED SOAP set cannot reach (no
# write verb + no escape hatch), named so a caller is TOLD the wall. Includes the
# two honesty-defect caps (AP_MODE, CHANNELS) — channel is read-only via get, AP
# mode has no SOAP write at all — alongside SSID/port-forward/IPv6/VPN.
_GUI_ONLY_CEILING: tuple[str, ...] = (
    "SSID name/password (no pynetgear SOAP write — Orbi app / web UI only)",
    "radio channel (CHANNELS: read-only; no set-channel SOAP action)",
    "AP/router operating mode (AP_MODE: no set-mode SOAP action)",
    "port-forwarding rules (no SOAP write)",
    "IPv6 configuration (no SOAP write)",
    "VPN server/client (no SOAP write)",
)


# Read-path vocabulary → the pynetgear getter that backs each. These reads return
# structured data (a dict, or a list of ``Device`` namedtuples / satellite dicts),
# so :meth:`OrbiProvider.get` serializes them to JSON (the scalar guest/channel/
# model/firmware reads stay special-cased above). Covers attached devices, the
# mesh satellites, WAN/IP, system info, the traffic meter, and guest-network info.
_READ_JSON: dict[str, str] = {
    "devices/attached": "get_attached_devices",
    "mesh/satellites": "get_satellites",
    "wan/ip": "get_wan_ip_con_info",
    "system/info": "get_system_info",
    "traffic/meter": "get_traffic_meter",
    "traffic/enabled": "get_traffic_meter_enabled",
    "guest_wifi/2g/info": "get_2g_guest_access_network_info",
    "guest_wifi/5g/info": "get_5g_guest_access_network_info",
}


def _to_jsonable(obj: Any) -> Any:
    """Coerce a pynetgear getter result into a JSON-serializable shape.

    Attached-device reads return ``Device`` *namedtuples*, which JSON would emit
    as bare arrays — convert them to dicts via ``_asdict`` so the fields stay
    addressable. Lists are mapped element-wise; plain dicts/scalars pass through.
    Non-native values the traffic-meter parser emits (``timedelta``) are handled
    by ``default=str`` at the ``json.dumps`` call site, not here.
    """
    if isinstance(obj, list):
        return [_to_jsonable(item) for item in obj]
    asdict = getattr(obj, "_asdict", None)
    if callable(asdict):  # a namedtuple (pynetgear ``Device``)
        return dict(asdict())
    return obj


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
        # Whether the last connect() genuinely authenticated. ``connect`` is
        # deliberately BEST-EFFORT (it never raises on a rejected/unreachable
        # login — we have no live creds yet, so the build must not block on a live
        # call), so "connect did not raise" is NOT proof of auth for this brand.
        # This flag records the real ``login()`` outcome so an auth-probe caller
        # (onboard's pairing gate) can positively verify the session is authed via
        # :meth:`auth_ok` instead of mis-reading a tolerated failure as success.
        self._authed: bool = False

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
        client = _make_client(authed)
        self._client = client
        self._authed = False
        # Login is best-effort: a failed/unreachable login must not raise at
        # connect (we have no live creds yet). The client is retained either way;
        # a later get/set raises DeviceError on the transport failure per contract.
        # ``pynetgear.login()`` returns a bool — False (or any falsey) means the
        # box rejected the creds, so we do NOT refine the brand off a session that
        # is not authed (a stale get_info read could otherwise mislead). Only a
        # truthy login refines ``brand`` to the concrete model AND records the
        # session as genuinely authenticated (see :meth:`auth_ok`).
        try:
            ok = client.login()
        except Exception:  # tolerate any login transport error (best-effort connect)
            return
        if ok:
            self._authed = True
            self._refine_brand()

    def auth_ok(self) -> bool:
        """True iff the last :meth:`connect` genuinely authenticated.

        The explicit auth oracle a read-only auth-probe (onboard's pairing gate)
        needs for this brand. Because :meth:`connect` is BEST-EFFORT — a wrong
        password or an unreachable box is swallowed and connect returns cleanly —
        "connect did not raise" is NOT proof the creds are good. A caller that
        infers pairing success from a non-raising connect would persist a false
        "paired" against a box it cannot authenticate to (the secret kept, a
        ``devices.orbi`` block written), which bites on the first real
        ``sanctum net orbi`` op. This returns the recorded ``login()`` outcome —
        the authoritative auth signal — so the gate can fail-close correctly: a
        rejected/unreachable Orbi yields ``False`` → revoke + no devices block.
        """
        return self._authed

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
        self._authed = False

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
        getter_name = _READ_JSON.get(path)
        if getter_name is not None:
            return self._read_json(getter_name)
        # Unknown path → best-effort None (a path this provider does not expose),
        # NOT an error — but only after _require_client() so a pre-connect call
        # still fails legibly rather than silently returning None.
        self._require_client()
        return None

    def _read_json(self, getter_name: str) -> str | None:
        """Call a structured-read pynetgear getter and serialize it to JSON.

        Backs the :data:`_READ_JSON` paths (attached devices, satellites, WAN/IP,
        system info, traffic meter, guest-network info). A getter returning
        ``None`` (a read error inside pynetgear) yields ``None`` (a best-effort
        unknown), while a transport/auth exception raises :class:`DeviceError` —
        matching the scalar reads' contract (a transport failure is never masked
        as an empty read). ``default=str`` lets the traffic-meter parser's
        non-native values (e.g. ``timedelta``) serialize without blowing up.
        """
        client = self._require_client()
        getter = getattr(client, getter_name)
        try:
            data = getter()
        except Exception as exc:  # normalize any transport error
            msg = f"Orbi {getter_name} read failed: {exc}"
            raise DeviceError(msg) from exc
        if data is None:
            return None
        return json.dumps(
            _to_jsonable(data),
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )

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

    def action(self, name: str, **kwargs: Any) -> OpResult:
        """Issue one wired pynetgear write/action by stable name, fail-closed.

        The generic dispatch over :data:`_ACTIONS` — Orbi's counterpart to
        Sagemcom's ``action`` escape hatch, EXCEPT pynetgear has NO escape hatch:
        ``name`` must be one of the FIXED wired actions (:meth:`list_actions`
        enumerates them), so an unknown name is refused (``ok=False``) rather than
        sent to a non-existent SOAP verb. The arg shape is the action's: a
        ``"value"`` toggle takes ``value=<bool>`` (handed to pynetgear as a Python
        bool — its ``value_to_zero_or_one`` RAISES on the ``"on"``/``"off"`` provider
        vocabulary), and a ``"mac"`` action takes ``mac=<str>, allow=<bool>``
        (mapped to ``device_status="Allow"|"Block"``).

        Fail-closed exactly like Sagemcom's ``set``/``reboot`` (Contracts at the
        Boundary): every wired write rides ``Netgear._set``, which **RETURNS a
        bool** — ``False`` on a rejected/failed write, WITHOUT raising. So "the
        call did not raise" is NOT proof the write landed: a falsey return yields
        ``ok=False`` (never a green ``ok=True``), so the rails trip rollback. A
        transport *exception* normalizes to :class:`DeviceError`.
        """
        client = self._require_client()
        spec = _ACTIONS.get(name)
        if spec is None:
            return OpResult(
                ok=False,
                detail=(
                    f"orbi: unknown action: {name!r} "
                    "(pynetgear has no escape hatch; only the wired actions exist)"
                ),
            )
        fn = getattr(client, spec.method)
        # Derive the bound args + audit ``after`` from the arg shape BEFORE the
        # transport call, so a missing argument is a clean caller error (a
        # DeviceError), never confused with the transport failure handled below.
        value_arg: bool = False
        mac_arg: str = ""
        status_arg: str = _STATUS_BLOCK
        after: str | None = None
        if spec.arg == "value":
            if "value" not in kwargs:
                msg = f"Orbi action {name!r} requires a boolean 'value'"
                raise DeviceError(msg, fix="call action(name, value=True|False)")
            value_arg = bool(kwargs["value"])
            after = "on" if value_arg else "off"
        elif spec.arg == "mac":
            if "mac" not in kwargs or "allow" not in kwargs:
                msg = f"Orbi action {name!r} requires 'mac' and 'allow'"
                raise DeviceError(msg, fix="call action(name, mac=..., allow=True|False)")
            mac_arg = str(kwargs["mac"])
            status_arg = _STATUS_ALLOW if bool(kwargs["allow"]) else _STATUS_BLOCK
            after = f"{mac_arg}={status_arg.lower()}"
        try:
            if spec.arg == "value":
                ok = fn(value_arg)
            elif spec.arg == "mac":
                ok = fn(mac_arg, device_status=status_arg)
            else:
                ok = fn()
        except Exception as exc:  # normalize any transport error
            msg = f"Orbi action {name!r} failed: {exc}"
            raise DeviceError(
                msg, fix="check the box is reachable and the session is valid"
            ) from exc
        if not ok:
            # Netgear._set RETURNS False (no raise) on a rejected write — fail-
            # closed: a False is NOT a green success (the Orbi analog of
            # Sagemcom's _reply_error reply-inspector).
            return OpResult(
                ok=False,
                detail=(
                    f"orbi: {name} rejected by the router "
                    f"(SOAP {spec.method} returned no-success)"
                ),
            )
        return OpResult(ok=True, detail=name, after=after)

    def reboot(self) -> OpResult:
        """Reboot the router via the SOAP ``reboot`` action (REBOOT cap)."""
        return self.action("reboot")

    def update_firmware(self) -> OpResult:
        """Trigger a firmware update via ``update_new_firmware`` (FIRMWARE cap)."""
        return self.action("update_firmware")

    def set_qos(self, enabled: bool) -> OpResult:
        """Enable/disable QoS via ``set_qos_enable_status`` (FEATURE_TOGGLE cap)."""
        return self.action("set_qos", value=enabled)

    def set_smart_connect(self, enabled: bool) -> OpResult:
        """Enable/disable Smart Connect via ``set_smart_connect_enabled``."""
        return self.action("set_smart_connect", value=enabled)

    def set_traffic_meter(self, enabled: bool) -> OpResult:
        """Enable/disable the traffic meter via ``enable_traffic_meter``."""
        return self.action("set_traffic_meter", value=enabled)

    def set_block_device_enable(self, enabled: bool) -> OpResult:
        """Toggle the access-control feature via ``set_block_device_enable`` (POLICY)."""
        return self.action("set_block_device_enable", value=enabled)

    def allow_block_device(self, mac: str, *, allow: bool) -> OpResult:
        """Allow/block one device by MAC via ``allow_block_device`` (POLICY cap).

        The per-MAC write: ``allow=False`` blocks the device
        (``device_status="Block"``), ``allow=True`` re-allows it
        (``device_status="Allow"``) — pynetgear's real positional+keyword shape.
        """
        return self.action("allow_block_device", mac=mac, allow=allow)

    def speed_test(self) -> OpResult:
        """Start a speed test via ``set_speed_test_start`` (SPEEDTEST cap)."""
        return self.action("speed_test")

    def list_actions(self) -> list[OrbiAction]:
        """Enumerate the wired pynetgear write/action surface — Orbi's discovery.

        ``pynetgear`` ships a FIXED set of SOAP methods with NO escape hatch and NO
        discovery, so — unlike Sagemcom's subtree-walk ``discover`` — the writable
        surface is statically enumerable: this returns every :class:`OrbiAction`
        the provider wraps (name → concrete pynetgear method, backing capability,
        arg shape), sorted by name. Each named method is a REAL ``Netgear`` method,
        and each capability is one :meth:`capabilities` advertises.
        """
        return sorted(_ACTIONS.values(), key=lambda action: action.name)

    def capabilities(self) -> AbstractSet[Capability]:
        """Operations this Orbi actually supports — derived from the wired surface.

        Honest-verify: the write capabilities are computed straight from
        :data:`_ACTIONS` (REBOOT/FIRMWARE/FEATURE_TOGGLE/POLICY/SPEEDTEST), so a
        cap is advertised IFF a real wired write backs it — the two can never drift
        into a phantom cap. ``READ`` (the getters) and ``GUEST_WIFI`` (the
        ``set_*_guest_access_enabled`` setter + capability_op) are the only caps not
        backed by an ``_ACTIONS`` entry, so they are added explicitly.

        ``AP_MODE`` and ``CHANNELS`` are NEVER advertised: ``pynetgear``'s fixed
        SOAP actions include NO write for the AP/router operating mode and NO write
        for the radio channel (no set-AP-mode / set-channel verb), and there is no
        raw escape hatch to reach an unmodeled leaf — advertising either would name
        an op that does not exist. The channel leaves remain READ-only via ``get``.
        """
        caps: set[Capability] = {Capability.READ, Capability.GUEST_WIFI}
        caps |= {action.capability for action in _ACTIONS.values()}
        return caps

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        """The Orbi-specific (path, engaged) binding for ``capability``, or None.

        This is the brand-owned vocabulary a Layer-2 intent reaches through, so
        the intent never hardcodes an Orbi path. ``GUEST_WIFI`` binds to the 5 GHz
        guest leaf; every other capability returns ``None`` (no blind mutation).
        """
        return _CAPABILITY_OPS.get(capability)

    def fallback_transport(self) -> TransportKind:
        """The GUI transport for the Orbi's GUI-only ceiling: agent-browser.

        An Orbi exposes BOTH a web admin UI (``orbilogin.com`` / the gateway) and
        the Orbi mobile app, so per the API→agent-browser→android priority chain the
        web UI (agent-browser) wins as the fallback for the surfaces pynetgear's
        fixed SOAP set cannot reach (SSID/channel/AP-mode/port-forward/IPv6/VPN —
        incl. the AP_MODE + CHANNELS honesty defects). The multi-transport router
        reads this via the optional
        :class:`~sanctum_cli.devices.transport.FallbackTransportProvider` protocol.
        """
        return TransportKind.BROWSER

    def capability_map(self) -> CapabilityMap:
        """Honest "what can I change on this Orbi": real SOAP ops + the GUI-only ceiling.

        Every cap :meth:`capabilities` advertises is bound to the concrete
        ``pynetgear`` method(s) that back it (derived from :data:`_ACTIONS`, so the
        map cannot name a SOAP verb the wired surface does not have). The ceiling
        names SSID/channel/AP-mode/port-forward/IPv6/VPN — the surfaces pynetgear's
        FIXED action set cannot write (no verb, no escape hatch) — so AP_MODE and
        CHANNELS are reported as a wall, never as a phantom op. ``build_capability_map``
        enforces bindings ≡ ``capabilities()``.
        """
        return build_capability_map(
            brand=self.brand,
            capabilities=self.capabilities(),
            bindings=_CAP_BINDINGS,
            ceiling=_GUI_ONLY_CEILING,
        )

    def list_paths(self) -> list[CapabilityBinding]:
        """The flat list of REAL pynetgear bindings (the writable/readable surface)."""
        return list(self.capability_map().bindings)

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
