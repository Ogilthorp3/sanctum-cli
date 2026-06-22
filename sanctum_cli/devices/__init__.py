"""Network-gear device providers — Layer-1 control surface + Layer-2 intents.

Public exports are the base contract types so ``from sanctum_cli.devices import
DeviceProvider`` works. Heavier modules (registry, sagemcom, rails, intents) are
imported from their submodules directly to keep this package marker import-cheap.
"""

from __future__ import annotations

from sanctum_cli.devices.base import (
    Capability,
    Creds,
    DeviceError,
    DeviceProvider,
    NetContext,
    OpResult,
    Snapshot,
)

__all__ = [
    "Capability",
    "Creds",
    "DeviceError",
    "DeviceProvider",
    "NetContext",
    "OpResult",
    "Snapshot",
]
