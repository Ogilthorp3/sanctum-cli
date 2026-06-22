"""Layer-1 device-provider contract.

Every brand of network gear (hubs, firewalls, mesh APs) is driven through a
single uniform :class:`DeviceProvider` Protocol — full control, brand-specific
transport hidden behind it. Layer-2 apple-like intents (see
:mod:`sanctum_cli.devices.intents`) compose providers with snapshot→confirm→
verify→rollback rails; a registry (see :mod:`sanctum_cli.devices.registry`)
resolves ``kind → provider`` via per-provider ``detect()`` fingerprints.

This module is pure types — no I/O, no network — so it stays import-cheap and
trivially testable. The frozen dataclasses are the wire format between layers;
the Protocol is the seam a contributor implements to add a new brand.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from sanctum_cli.errors import LocalError

# A command runner: maps an opaque arg tuple to its stdout. Mirrors
# ``sanctum_cli.net.detect.Runner`` so a NetContext built by the net layer
# can be threaded straight into a provider's ``detect()``.
Runner = Callable[[tuple[str, ...]], str]


class Capability(StrEnum):
    """What a provider can actually do, so callers can gate before they try.

    A provider advertises its real surface via :meth:`DeviceProvider.capabilities`;
    intents and the CLI check membership before composing a mutating change so an
    unsupported op fails fast (and legibly) instead of mid-transport.
    """

    READ = "read"
    SET = "set"
    BRIDGE_MODE = "bridge_mode"
    DMZ = "dmz"
    WAN_MODE = "wan_mode"
    REBOOT = "reboot"
    FIRMWARE = "firmware"
    POLICY = "policy"
    SCREEN_TIME = "screen_time"
    WIFI = "wifi"
    AP_MODE = "ap_mode"
    CHANNELS = "channels"
    GUEST_WIFI = "guest_wifi"


@dataclass(frozen=True)
class Creds:
    """How to authenticate to one device.

    ``secret`` is a password/token read from the Keychain at call time (never
    persisted by sanctum-cli); ``key_path`` is an SSH key path for transports
    that authenticate with a key instead. Exactly which the provider uses is the
    provider's business — both fields are optional so a single shape covers
    password-auth hubs and key-auth firewalls alike.

    ``keychain_service`` carries the *resolved* Keychain service name a
    password-auth provider should read its secret under (the ``username`` is the
    resolved Keychain *account*). It is the seam that lets a haus override
    ``devices.<kind>.keychain.service`` reach the provider's Keychain read,
    instead of the provider hardcoding its brand's service constant. ``None``
    means "the caller did not resolve a service" — a provider then falls back to
    its own per-brand default, so a direct ``connect`` (e.g. in a test) and the
    default haus path are unchanged.
    """

    host: str
    username: str
    secret: str | None = None
    key_path: str | None = None
    keychain_service: str | None = None


@dataclass(frozen=True)
class NetContext:
    """The local network facts a provider's ``detect()`` reasons over.

    ``gateway_ip`` is the parsed default gateway (e.g. from
    ``net.detect.parse_default_gateway``); ``runner`` is the same command-runner
    abstraction the net layer uses, so ``detect()`` can probe without owning its
    own subprocess plumbing. Both are optional — a context with neither still
    lets the registry fall back to the generic read-only provider.
    """

    gateway_ip: str | None
    runner: Runner | None


@dataclass(frozen=True)
class Snapshot:
    """An immutable capture of the device state we may need to restore.

    ``data`` is an opaque path→value map the *same* provider understands; only
    the provider that took the snapshot knows how to ``rollback`` it. ``taken_at``
    is an ISO-8601 stamp for the audit trail.
    """

    brand: str
    taken_at: str
    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OpResult:
    """The outcome of a single mutating op, carrying before/after for the audit log."""

    ok: bool
    detail: str
    before: str | None = None
    after: str | None = None


@dataclass(frozen=True)
class CapabilityOp:
    """A brand-specific binding for a high-level :class:`Capability`.

    The seam that keeps Layer-2 intents brand-agnostic: an intent names a
    *capability* (``Capability.BRIDGE_MODE``), and the provider returns the
    concrete ``path`` + ``engaged`` value that achieves it on *this* brand. So a
    Bell/Sagemcom hub maps ``BRIDGE_MODE`` to
    ``("Device/Services/BellNetworkCfg/SetBridgeMode", "on")`` while a SOAP Orbi
    would map the same capability to its own path/value — and the intent code,
    the snapshot baseline, and the wizard never learn either brand's vocabulary.

    ``path`` is the provider-specific leaf the intent ``set``s; ``engaged`` is the
    value that turns the capability *on*. Both are owned entirely by the brand.
    """

    path: str
    engaged: str


class DeviceError(LocalError):
    """A device transport/op failed, or an op is unsupported by this provider.

    A :class:`sanctum_cli.errors.LocalError` subclass so it maps to the
    ``LOCAL_ERROR`` exit code and inherits the ``fix=`` suggestion channel.
    """


@runtime_checkable
class DeviceProvider(Protocol):
    """Layer-1 uniform control surface every brand implements.

    Implementations need not subclass this — structural conformance is enough,
    and ``@runtime_checkable`` lets the registry/tests ``isinstance``-check a
    candidate. Class attributes ``kind`` (``"hub"``/``"firewall"``/...) and
    ``brand`` identify the provider; ``detect`` is a staticmethod so the registry
    can fingerprint a network without instantiating credentials first.
    """

    kind: str
    brand: str

    @staticmethod
    def detect(net: NetContext) -> float:
        """Confidence in ``[0.0, 1.0]`` that this provider drives the device at ``net``."""
        ...

    def connect(self, creds: Creds | None) -> None:
        """Open an authenticated session (or no-op for credential-less providers)."""
        ...

    def disconnect(self) -> None:
        """Release any transport this provider holds (session, loop, socket).

        The uniform lifecycle-close primitive: a consumer that programs against
        the :class:`DeviceProvider` type can deterministically release a
        provider's resources without knowing the brand. MUST be idempotent and
        safe to call when never connected (so a ``finally`` block can always call
        it). Credential-less / transport-less providers implement it as a no-op.
        """
        ...

    def get(self, path: str) -> str | None:
        """Read one value addressed by a provider-specific ``path``.

        Returns ``None`` when the path is unknown to this provider — a normal,
        best-effort outcome (e.g. the read-only generic fallback, or a path the
        device does not expose), not an error. Transport/auth failures raise
        :class:`DeviceError`.
        """
        ...

    def set(self, path: str, value: str) -> OpResult:
        """Write one value; return an :class:`OpResult` with before/after."""
        ...

    def capabilities(self) -> AbstractSet[Capability]:
        """The set of operations this provider actually supports.

        Typed as an abstract ``Set`` (not the builtin ``set``) so the annotation
        does not collide with the ``set`` *method* defined above in class scope —
        and so implementations may return any set-like (``set``, ``frozenset``).
        """
        ...

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        """Map a high-level :class:`Capability` to this brand's concrete op.

        Returns the brand-specific ``(path, engaged)`` binding a Layer-2 intent
        uses to engage ``capability``, or ``None`` when this provider does not
        support it. This is the seam that keeps intents brand-agnostic: the
        intent asks for ``BRIDGE_MODE`` and the provider supplies the path/value —
        so adding a brand is one new ``devices/<brand>.py`` + a registry line,
        with no Layer-2 or wizard change (spec success criterion #1).
        """
        ...

    def snapshot(self, scope: str | None = None) -> Snapshot:
        """Capture restorable state, optionally narrowed to ``scope``."""
        ...

    def rollback(self, snap: Snapshot) -> OpResult:
        """Restore the device to a previously captured :class:`Snapshot`."""
        ...
