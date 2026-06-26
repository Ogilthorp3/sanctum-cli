"""Multi-transport routing — API now, agent-browser → android as Phase-2 fallbacks.

A high-level :class:`~sanctum_cli.devices.base.Capability` can be reached by more
than one transport. The Layer-1 :class:`~sanctum_cli.devices.base.DeviceProvider`
surface is the **API** transport — the only one wired LIVE today (Sagemcom
``sah:setValue``, Firewalla ``bridge-http``, Orbi ``pynetgear-soap``). But every
brand has a writability *ceiling* (see
:class:`~sanctum_cli.devices.base.CapabilityMap`) — surfaces the API genuinely
cannot reach (Orbi SSID/channel/AP-mode, Firewalla NAT/DMZ/WAN/VPN, Sagemcom's
carrier-locked leaves). Those need a GUI transport: the device's web admin UI
(driven by **agent-browser**) or its mobile app (**android**).

This module adds the seam that, per Capability, picks the transport:

* the **API** transport when the provider advertises a REAL op for the capability
  (it is in :meth:`~sanctum_cli.devices.base.DeviceProvider.capabilities`); else
* the brand's **GUI fallback** transport (agent-browser, then android — the fixed
  :data:`PRIORITY` chain) for a ceiling surface.

The GUI transports are Phase-2 STUBS: :meth:`GuiRecipeTransport.execute` raises
``NotImplementedError('Phase 2: live recipe')`` — no live UI/app mutation is
implemented here. They ARE cred-resolved, though: each resolves its secret through
the headless resolver (:func:`sanctum_cli.devices.creds.resolve_secret_optional`,
Keychain → SOPS → NEVER 1Password/op) so it is *authenticated-ready* the moment a
live recipe lands.

Honesty by construction: because the API routes are derived from the provider's
OWN ``capabilities()`` / ``capability_map()`` (themselves honest-verified — a cap
iff a real op backs it), a capability with no live op — the Orbi ``AP_MODE`` /
``CHANNELS`` and Firewalla ``WAN_MODE`` honesty defects — can ONLY be routed to a
GUI fallback, never advertised as a live transport it lacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sanctum_cli.devices import creds as creds_resolver
from sanctum_cli.devices.base import (
    Capability,
    CapabilityMapProvider,
    OpResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sanctum_cli.devices.base import DeviceProvider


class TransportKind(StrEnum):
    """How a setting is driven: the live API, or a Phase-2 GUI fallback.

    A :class:`StrEnum` so the value renders directly in the CLI and serializes
    cleanly. ``API`` is the only transport wired live today; ``BROWSER``
    (agent-browser, the device web UI) and ``ANDROID`` (the mobile app) are the
    Phase-2 fallbacks, ordered by the :data:`PRIORITY` chain.
    """

    API = "api"
    BROWSER = "agent-browser"
    ANDROID = "android"


#: The fixed transport-selection priority: prefer the live API, then the web UI
#: (agent-browser), then the mobile app (android). The router walks this order —
#: API wins whenever a real op backs the capability; otherwise the brand's GUI
#: fallback (its first reachable non-API rung) is selected.
PRIORITY: tuple[TransportKind, ...] = (
    TransportKind.API,
    TransportKind.BROWSER,
    TransportKind.ANDROID,
)

#: The marker a Phase-2 GUI recipe raises / a ceiling route carries — the live UI/
#: app mutation is intentionally not implemented in this scaffold.
PHASE2_RECIPE_MSG = "Phase 2: live recipe"


@runtime_checkable
class FallbackTransportProvider(Protocol):
    """An OPTIONAL provider capability: which GUI transport reaches its ceiling.

    A brand declares whether its GUI-only surfaces live behind a web admin UI
    (``agent-browser``) or a mobile app (``android``). Like
    :class:`~sanctum_cli.devices.base.AuthProbeProvider`, it is a SEPARATE
    ``@runtime_checkable`` Protocol (not part of the core ``DeviceProvider``) so a
    provider that does not declare one is not forced to — :func:`fallback_kind`
    structurally checks and defaults to the priority chain's first non-API rung
    (agent-browser). Keeping the choice in the PROVIDER (not the router) keeps the
    router brand-agnostic: a Firewalla — which has NO admin web UI, only the
    mobile app — returns ``ANDROID``; a Bell hub / Orbi, which have web admin UIs,
    return ``BROWSER``.
    """

    def fallback_transport(self) -> TransportKind:
        """The GUI transport that reaches this brand's GUI-only ceiling."""
        ...


@runtime_checkable
class Transport(Protocol):
    """One executor for a capability over a specific channel.

    ``kind`` names the channel; ``live`` is True only for the API transport
    (browser/app are Phase-2 stubs). :meth:`authenticated` is a read-only oracle
    (does the transport hold a usable session / resolved credential), and
    :meth:`execute` performs the op — LIVE for the API transport, a Phase-2
    ``NotImplementedError`` for the GUI stubs.
    """

    kind: TransportKind
    live: bool

    def authenticated(self) -> bool:
        """True iff this transport holds a usable session / resolved credential."""
        ...

    def execute(self, capability: Capability, value: str | None = None, **params: Any) -> OpResult:
        """Drive ``capability`` over this transport (API live; GUI = Phase-2 stub)."""
        ...


class ApiTransport:
    """The LIVE "API (now)" transport — drives a capability through the provider.

    Wraps a connected :class:`~sanctum_cli.devices.base.DeviceProvider` and reaches
    a capability via the brand-owned vocabulary: :meth:`execute` resolves the
    provider's :meth:`~sanctum_cli.devices.base.DeviceProvider.capability_op` and
    issues the concrete ``set`` — so the transport never hardcodes a brand path and
    never fires a write for a capability the provider has no op for (it returns
    ``ok=False`` instead, never a phantom write). This is the raw executor the rails
    (:func:`sanctum_cli.devices.rails.guarded_apply`) wrap when a mutation must be
    snapshot/verify/rollback-guarded; the read-only ``capabilities`` listing never
    calls it.
    """

    kind = TransportKind.API
    live = True

    def __init__(self, provider: DeviceProvider) -> None:
        self._provider = provider

    def authenticated(self) -> bool:
        """Delegate to the provider's auth oracle when it has one, else assume up.

        A provider whose ``connect`` is fail-closed (Sagemcom) sets its client only
        on a genuine login, so its :meth:`auth_ok` is authoritative; a best-effort
        ``connect`` (Orbi) records the real login outcome the same way. A provider
        with no :class:`~sanctum_cli.devices.base.AuthProbeProvider` surface is
        treated as authenticated (its ``connect`` raised on failure).
        """
        from sanctum_cli.devices.base import AuthProbeProvider

        if isinstance(self._provider, AuthProbeProvider):
            return self._provider.auth_ok()
        return True

    def execute(self, capability: Capability, value: str | None = None, **params: Any) -> OpResult:
        """Issue ``capability`` through the provider's ``capability_op`` + ``set``.

        ``value`` overrides the value written; when omitted the capability_op's
        ``engaged`` (the "turn it on" value) is used. A capability the provider
        exposes no op for yields ``ok=False`` (never a blind write to a hardcoded
        path). ``params`` is accepted for symmetry with the GUI recipe signature but
        is unused by the single-leaf ``set`` path.
        """
        del params  # unused by the single-leaf set path; kept for signature parity
        op = self._provider.capability_op(capability)
        if op is None:
            return OpResult(
                ok=False,
                detail=f"api: provider {self._provider.brand!r} has no op for {capability.value}",
            )
        return self._provider.set(op.path, value if value is not None else op.engaged)


class GuiRecipeTransport:
    """A Phase-2 GUI transport stub — authenticated-ready, but not yet live.

    Stands in for a web-UI (agent-browser) or mobile-app (android) recipe that
    drives a GUI-only ceiling surface. :meth:`execute` RAISES
    ``NotImplementedError('Phase 2: live recipe')`` — no live UI/app mutation is
    implemented in this scaffold. It IS cred-resolved: :meth:`authenticate`
    resolves the device secret through the headless resolver
    (:func:`sanctum_cli.devices.creds.resolve_secret_optional` by default —
    Keychain → SOPS → NEVER 1Password/op, whose TouchID prompt would block a
    headless daemon), so when a Phase-2 recipe lands the credential is already
    wired. ``resolver`` is injectable so a test can drive the wiring without a real
    Keychain/SOPS.
    """

    live = False

    def __init__(
        self,
        kind: TransportKind,
        *,
        account: str,
        service: str,
        resolver: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self.kind = kind
        self._account = account
        self._service = service
        self._resolver = resolver if resolver is not None else creds_resolver.resolve_secret_optional
        self._secret: str | None = None
        self._resolved = False

    def authenticate(self) -> bool:
        """Resolve the device secret through the headless resolver (idempotent).

        Keychain → SOPS → NEVER op (the resolver's own guaranteed order). Returns
        True iff a secret came back under the wired ``(account, service)``. Safe to
        call repeatedly — the resolution is memoized after the first call.
        """
        if not self._resolved:
            self._secret = self._resolver(self._account, self._service)
            self._resolved = True
        return self._secret is not None

    def authenticated(self) -> bool:
        """True iff a credential resolved (triggers a lazy :meth:`authenticate`)."""
        return self.authenticate()

    def execute(self, capability: Capability, value: str | None = None, **params: Any) -> OpResult:
        """Phase-2 stub: the live UI/app recipe is intentionally not implemented.

        Raises ``NotImplementedError('Phase 2: live recipe')`` rather than silently
        no-op'ing, so a caller can never mistake an un-implemented GUI recipe for a
        successful mutation. The transport is authenticated-ready (creds resolve via
        the headless resolver), so wiring a real recipe here is the only remaining
        Phase-2 step.
        """
        del capability, value, params  # a live recipe will consume these in Phase 2
        raise NotImplementedError(PHASE2_RECIPE_MSG)


@dataclass(frozen=True)
class CapabilityRoute:
    """One row of the transport plan: a setting → the transport that drives it.

    ``setting`` is the capability value (an API route) or the ceiling surface text
    (a GUI-only route); ``capability`` carries the
    :class:`~sanctum_cli.devices.base.Capability` for an API route and ``None`` for
    a ceiling surface (which is free-form, not an advertised cap). ``transport`` is
    the selected channel; ``op`` is the concrete real op (API) or
    :data:`PHASE2_RECIPE_MSG` (GUI stub); ``live`` is True only for an API route.
    """

    setting: str
    transport: TransportKind
    op: str
    live: bool
    capability: Capability | None


@dataclass(frozen=True)
class RoutePlan:
    """The full per-setting transport plan for one box.

    ``routes`` are the API routes (one per advertised capability, ``live=True``)
    followed by the GUI-only ceiling routes (one per named ceiling surface,
    ``live=False`` Phase-2 stubs). ``fallback`` is the brand's GUI transport for the
    ceiling — agent-browser for web-UI brands, android for an app-only box.
    """

    brand: str
    fallback: TransportKind
    routes: tuple[CapabilityRoute, ...]


def fallback_kind(provider: DeviceProvider) -> TransportKind:
    """The GUI fallback transport for ``provider`` (brand-declared, else default).

    A provider implementing :class:`FallbackTransportProvider` names its own GUI
    transport (the brand knows whether its ceiling lives behind a web UI or an
    app); any other provider defaults to the priority chain's first non-API rung,
    agent-browser. Keeps the router brand-agnostic — the browser-vs-app choice is
    the provider's, not the router's.
    """
    if isinstance(provider, FallbackTransportProvider):
        return provider.fallback_transport()
    return TransportKind.BROWSER


def select_transport(provider: DeviceProvider, capability: Capability) -> TransportKind:
    """Pick the transport for ``capability`` on ``provider`` (API now, else GUI).

    API whenever the provider advertises a REAL op for the capability (it is in
    :meth:`~sanctum_cli.devices.base.DeviceProvider.capabilities` — itself
    honest-verified, so a cap is present iff a real op backs it); otherwise the
    brand's GUI fallback (:func:`fallback_kind`). This is what makes the 3 honesty
    defects route correctly: AP_MODE/CHANNELS (Orbi) and WAN_MODE (Firewalla) are
    absent from ``capabilities()`` — no live op — so they can only resolve to a GUI
    fallback, never a live API transport they lack.
    """
    if capability in provider.capabilities():
        return TransportKind.API
    return fallback_kind(provider)


def build_transport(
    provider: DeviceProvider,
    kind: TransportKind,
    *,
    account: str = "",
    service: str = "",
    resolver: Callable[[str, str], str | None] | None = None,
) -> Transport:
    """Construct the executor for ``kind`` (live API, or a cred-ready GUI stub).

    ``TransportKind.API`` wraps ``provider`` in a live :class:`ApiTransport`; any GUI
    kind returns a :class:`GuiRecipeTransport` wired with ``(account, service)`` so
    it is authenticated-ready via the headless resolver. ``resolver`` is injectable
    for tests; it defaults to the headless
    :func:`sanctum_cli.devices.creds.resolve_secret_optional` (Keychain → SOPS →
    never op).
    """
    if kind is TransportKind.API:
        return ApiTransport(provider)
    return GuiRecipeTransport(kind, account=account, service=service, resolver=resolver)


def plan_routes(provider: DeviceProvider) -> RoutePlan:
    """Build the full per-setting transport plan for ``provider``.

    For a provider with an honest :class:`~sanctum_cli.devices.base.CapabilityMap`
    (:class:`~sanctum_cli.devices.base.CapabilityMapProvider`): one live API route
    per real binding (carrying the binding's concrete transport + op), then one
    Phase-2 GUI route per named ceiling surface (routed to the brand's
    :func:`fallback_kind`). For a degraded provider with no map (the read-only
    fallback), the advertised ``capabilities()`` are listed as API routes with no
    ceiling — there is no honest ceiling to claim.

    The API routes are derived from the provider's OWN map (a different source than
    this router — Contracts at the Boundary), so the plan can neither invent a live
    op nor drop an advertised one: it mirrors the honest-verified surface exactly.
    """
    fallback = fallback_kind(provider)
    routes: list[CapabilityRoute] = []
    if isinstance(provider, CapabilityMapProvider):
        cmap = provider.capability_map()
        brand = cmap.brand
        for binding in cmap.bindings:
            routes.append(
                CapabilityRoute(
                    setting=binding.capability.value,
                    transport=TransportKind.API,
                    op=f"{binding.transport}: {binding.op}",
                    live=True,
                    capability=binding.capability,
                )
            )
        for surface in cmap.ceiling:
            routes.append(
                CapabilityRoute(
                    setting=surface,
                    transport=fallback,
                    op=PHASE2_RECIPE_MSG,
                    live=False,
                    capability=None,
                )
            )
    else:
        brand = provider.brand
        for cap in sorted(provider.capabilities(), key=lambda c: c.value):
            routes.append(
                CapabilityRoute(
                    setting=cap.value,
                    transport=TransportKind.API,
                    op="provider op",
                    live=True,
                    capability=cap,
                )
            )
    return RoutePlan(brand=brand, fallback=fallback, routes=tuple(routes))


__all__ = [
    "PHASE2_RECIPE_MSG",
    "PRIORITY",
    "ApiTransport",
    "CapabilityRoute",
    "FallbackTransportProvider",
    "GuiRecipeTransport",
    "RoutePlan",
    "Transport",
    "TransportKind",
    "build_transport",
    "fallback_kind",
    "plan_routes",
    "select_transport",
]
