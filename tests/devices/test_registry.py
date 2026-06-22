"""Registry: register → detect-ranked resolve → generic read-only fallback.

The registry is the seam that turns ``kind`` + a :class:`NetContext` into a
concrete provider without the caller knowing any brand. Each registered provider
fingerprints the network via its ``detect()`` staticmethod; ``resolve`` picks the
most-confident match. When nothing matches (unknown kind, or every ``detect``
returns ``0``) the caller still gets a usable object — a read-only provider that
lets reads through best-effort but refuses every mutation with a legible error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

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
    from collections.abc import Iterator


class FakeProvider:
    """A complete provider that always claims the network with full confidence."""

    kind = "hub"
    brand = "fake-hub"

    def __init__(self) -> None:
        self._v: dict[str, str] = {"WanMode": "gpon"}
        self._conn = False

    @staticmethod
    def detect(net: NetContext) -> float:
        return 1.0

    def connect(self, creds: Creds | None) -> None:
        self._conn = True

    def get(self, path: str) -> str:
        return self._v[path]

    def set(self, path: str, value: str) -> OpResult:
        before = self._v.get(path)
        self._v[path] = value
        return OpResult(ok=True, detail="set", before=before, after=value)

    def capabilities(self) -> set[Capability]:
        return {Capability.READ, Capability.SET}

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(brand=self.brand, taken_at="t", data=dict(self._v))

    def rollback(self, snap: Snapshot) -> OpResult:
        self._v = dict(snap.data)
        return OpResult(ok=True, detail="rolled back")


class WeakHub:
    """A second 'hub' provider with lower detect confidence than FakeProvider."""

    kind = "hub"
    brand = "weak-hub"

    @staticmethod
    def detect(net: NetContext) -> float:
        return 0.3

    def connect(self, creds: Creds | None) -> None:  # pragma: no cover - trivial
        return None

    def get(self, path: str) -> str:  # pragma: no cover - not exercised
        return ""

    def set(self, path: str, value: str) -> OpResult:  # pragma: no cover
        return OpResult(ok=True, detail="set")

    def capabilities(self) -> set[Capability]:  # pragma: no cover
        return {Capability.READ}

    def snapshot(self, scope: str | None = None) -> Snapshot:  # pragma: no cover
        return Snapshot(brand=self.brand, taken_at="t", data={})

    def rollback(self, snap: Snapshot) -> OpResult:  # pragma: no cover
        return OpResult(ok=True, detail="rolled back")


class NeverHub:
    """A 'hub' provider that never recognizes any network."""

    kind = "hub"
    brand = "never-hub"

    @staticmethod
    def detect(net: NetContext) -> float:
        return 0.0

    def connect(self, creds: Creds | None) -> None:  # pragma: no cover
        return None

    def get(self, path: str) -> str:  # pragma: no cover
        return ""

    def set(self, path: str, value: str) -> OpResult:  # pragma: no cover
        return OpResult(ok=True, detail="set")

    def capabilities(self) -> set[Capability]:  # pragma: no cover
        return {Capability.READ}

    def snapshot(self, scope: str | None = None) -> Snapshot:  # pragma: no cover
        return Snapshot(brand=self.brand, taken_at="t", data={})

    def rollback(self, snap: Snapshot) -> OpResult:  # pragma: no cover
        return OpResult(ok=True, detail="rolled back")


@pytest.fixture
def clean_registry() -> Iterator[None]:
    """Snapshot/restore the module-global registry so tests don't bleed."""
    from sanctum_cli.devices import registry

    saved = {k: list(v) for k, v in registry._REGISTRY.items()}
    registry._REGISTRY.clear()
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


def test_resolve_picks_highest_detect_then_generic(clean_registry: None) -> None:
    from sanctum_cli.devices import registry

    registry.register(FakeProvider)
    p = registry.resolve("hub", NetContext(gateway_ip="192.168.2.1", runner=None))
    assert p.brand == "fake-hub"
    # unknown kind → generic read-only
    g = registry.resolve("toaster", NetContext(gateway_ip=None, runner=None))
    with pytest.raises(DeviceError):
        g.set("x", "y")


def test_register_keys_by_kind(clean_registry: None) -> None:
    from sanctum_cli.devices import registry

    registry.register(FakeProvider)
    assert "hub" in registry._REGISTRY
    assert FakeProvider in registry._REGISTRY["hub"]


def test_register_is_idempotent(clean_registry: None) -> None:
    """Re-registering the same class must not duplicate it (import re-runs happen)."""
    from sanctum_cli.devices import registry

    registry.register(FakeProvider)
    registry.register(FakeProvider)
    assert registry._REGISTRY["hub"].count(FakeProvider) == 1


def test_resolve_ranks_by_confidence(clean_registry: None) -> None:
    from sanctum_cli.devices import registry

    # Register weak first so order alone cannot explain the win.
    registry.register(WeakHub)
    registry.register(FakeProvider)
    p = registry.resolve("hub", NetContext(gateway_ip="192.168.2.1", runner=None))
    assert p.brand == "fake-hub"


def test_resolve_all_zero_detect_falls_back_to_generic(clean_registry: None) -> None:
    from sanctum_cli.devices import registry

    registry.register(NeverHub)
    p = registry.resolve("hub", NetContext(gateway_ip="192.168.2.1", runner=None))
    # Nothing detected → generic read-only, not the NeverHub.
    assert p.brand != "never-hub"
    with pytest.raises(DeviceError):
        p.set("x", "y")


def test_generic_is_read_only_for_every_mutation(clean_registry: None) -> None:
    from sanctum_cli.devices import registry

    g = registry.resolve("toaster", NetContext(gateway_ip=None, runner=None))
    # capabilities is read-only
    assert g.capabilities() == {Capability.READ}
    # get is best-effort and returns None when nothing is known
    assert g.get("anything") is None
    with pytest.raises(DeviceError):
        g.set("a", "b")
    with pytest.raises(DeviceError):
        g.snapshot()
    with pytest.raises(DeviceError):
        g.rollback(Snapshot(brand="x", taken_at="t", data={}))


def test_generic_error_names_brand_and_invites_contribution(clean_registry: None) -> None:
    from sanctum_cli.devices import registry

    g = registry.resolve("toaster", NetContext(gateway_ip=None, runner=None))
    with pytest.raises(DeviceError) as ei:
        g.set("a", "b")
    msg = str(ei.value)
    assert "read-only" in msg
    assert "toaster" in msg
    assert "contribute" in msg


def test_resolve_returns_a_device_provider(clean_registry: None) -> None:
    from sanctum_cli.devices import registry

    registry.register(FakeProvider)
    p = registry.resolve("hub", NetContext(gateway_ip="192.168.2.1", runner=None))
    assert isinstance(p, DeviceProvider)
    g = registry.resolve("toaster", NetContext(gateway_ip=None, runner=None))
    assert isinstance(g, DeviceProvider)


def test_generic_exported_from_registry() -> None:
    from sanctum_cli.devices.registry import GenericReadOnlyProvider

    g = GenericReadOnlyProvider("orbi")
    assert g.kind == "orbi"
    assert "orbi" in g.brand
