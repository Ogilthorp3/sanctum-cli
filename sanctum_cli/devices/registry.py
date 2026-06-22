"""Provider registry: ``kind`` + network → the right brand, or a safe fallback.

Brands self-register their provider class via :func:`register`; the class's
``kind`` (``"hub"``/``"firewall"``/...) is the bucket key. :func:`resolve` is the
single entry point a caller uses: given a ``kind`` and a :class:`NetContext`, it
asks every candidate's ``detect()`` how confident it is that it drives the gear on
this network, and returns the most-confident match.

When nothing matches — an unknown ``kind``, or every ``detect()`` returns ``0`` —
the caller still gets a working object: :class:`GenericReadOnlyProvider`. It lets
reads through best-effort (returning ``None`` when it knows nothing) but refuses
every mutation with a legible :class:`DeviceError` that names the unsupported
brand and invites a contribution. That keeps ``sanctum net hub status`` useful on
gear nobody has written a driver for, while making it impossible to *change*
state through a driver that doesn't actually understand the device.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.devices.base import (
    Capability,
    Creds,
    DeviceError,
    DeviceProvider,
    NetContext,
    OpResult,
    Snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

# kind → registered provider classes for that kind. Module-global so providers
# register at import time; tests snapshot/restore it for isolation.
_REGISTRY: dict[str, list[type[DeviceProvider]]] = {}


def register(cls: type[DeviceProvider]) -> type[DeviceProvider]:
    """Register a provider class under its ``kind``.

    Idempotent: registering the same class twice (e.g. a module re-imported under
    a test runner) does not duplicate it. Returns the class so it can be used as a
    decorator. ``cls.kind`` is read off the class attribute the Protocol mandates.
    """
    bucket = _REGISTRY.setdefault(cls.kind, [])
    if cls not in bucket:
        bucket.append(cls)
    return cls


def resolve(kind: str, net: NetContext) -> DeviceProvider:
    """Return the most-confident provider for ``kind`` on ``net``.

    Each registered provider for ``kind`` is instantiated and its ``detect(net)``
    is scored; the highest score *strictly above zero* wins. If no provider is
    registered for ``kind``, or every ``detect`` returns ``0`` (nothing recognized
    the network), a :class:`GenericReadOnlyProvider` for ``kind`` is returned so
    the caller always gets a usable, read-only object.
    """
    best: DeviceProvider | None = None
    best_score = 0.0
    for cls in _REGISTRY.get(kind, []):
        candidate = cls()
        score = cls.detect(net)
        if score > best_score:
            best_score = score
            best = candidate
    if best is None:
        return GenericReadOnlyProvider(kind)
    return best


class GenericReadOnlyProvider:
    """Degraded fallback for gear no registered provider claims.

    Conforms to :class:`DeviceProvider` structurally so the registry can hand it
    back transparently, but it is deliberately *read-only*: ``get`` is best-effort
    and returns ``None`` when it has no value, while every mutating method raises
    :class:`DeviceError`. ``brand`` is derived from the requested ``kind`` so the
    error message can name what the user was actually pointing at.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.brand = f"generic-{kind}"

    # The args below are mandated by the DeviceProvider contract but unused in
    # this deliberately degraded fallback; noqa keeps the shape Protocol-conformant.

    @staticmethod
    def detect(net: NetContext) -> float:  # noqa: ARG004
        """The generic provider never *claims* a network — it is only a fallback."""
        return 0.0

    def connect(self, creds: Creds | None) -> None:  # noqa: ARG002
        """No session to open — the generic provider holds no transport."""
        return None

    def get(self, path: str) -> str | None:  # noqa: ARG002
        """Best-effort read. Knows nothing, so always returns ``None``."""
        return None

    def _refuse(self) -> DeviceError:
        return DeviceError(
            f"read-only: no provider for {self.kind}; contribute one",
            fix=(
                "Implement a DeviceProvider for this device and register it via "
                "sanctum_cli.devices.registry.register()."
            ),
        )

    def set(self, path: str, value: str) -> OpResult:  # noqa: ARG002
        raise self._refuse()

    def capabilities(self) -> AbstractSet[Capability]:
        return {Capability.READ}

    def snapshot(self, scope: str | None = None) -> Snapshot:  # noqa: ARG002
        raise self._refuse()

    def rollback(self, snap: Snapshot) -> OpResult:  # noqa: ARG002
        raise self._refuse()
