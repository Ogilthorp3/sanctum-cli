from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.devices.base import (
    Capability,
    CapabilityOp,
    Creds,
    DeviceError,
    DeviceProvider,
    NetContext,
    OpResult,
    Snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


class FakeProvider:
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

    def disconnect(self) -> None:
        self._conn = False

    def get(self, path: str) -> str:
        return self._v[path]

    def set(self, path: str, value: str) -> OpResult:
        before = self._v.get(path)
        self._v[path] = value
        return OpResult(ok=True, detail="set", before=before, after=value)

    def capabilities(self) -> set[Capability]:
        return {Capability.READ, Capability.SET}

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        if capability is Capability.BRIDGE_MODE:
            return CapabilityOp(path="WanMode", engaged="bridge")
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(brand=self.brand, taken_at="t", data=dict(self._v))

    def rollback(self, snap: Snapshot) -> OpResult:
        self._v = dict(snap.data)
        return OpResult(ok=True, detail="rolled back")


def test_fake_provider_satisfies_protocol() -> None:
    p: DeviceProvider = FakeProvider()
    assert isinstance(p, DeviceProvider)  # runtime_checkable
    p.connect(None)
    snap = p.snapshot()
    assert p.set("WanMode", "xgspon").after == "xgspon"
    p.rollback(snap)
    assert p.get("WanMode") == "gpon"


def test_capability_is_str_enum() -> None:
    # StrEnum members compare equal to their string value.
    assert Capability.READ == "read"
    assert Capability.WAN_MODE == "wan_mode"
    expected = {
        "READ",
        "SET",
        "BRIDGE_MODE",
        "DMZ",
        "WAN_MODE",
        "REBOOT",
        "FIRMWARE",
        "POLICY",
        "SCREEN_TIME",
        "WIFI",
        "AP_MODE",
        "CHANNELS",
        "GUEST_WIFI",
        # Firewalla bridge-backed named ops (each backed by a real route-correct op).
        "DEVICE_BLOCK",
        "DEVICE_POLICY",
        "DEVICE_RULES",
        "FEATURE_TOGGLE",
        "LOCAL_DNS",
        "ALARM_ACK",
        "WAKE_ON_LAN",
        "SPEEDTEST",
    }
    assert {c.name for c in Capability} == expected


def test_creds_is_frozen() -> None:
    c = Creds(host="192.168.2.1", username="admin", secret=None, key_path=None)
    assert c.host == "192.168.2.1"
    assert c.username == "admin"
    assert c.secret is None
    assert c.key_path is None
    assert c.keychain_service is None  # defaults to None (caller resolved nothing)
    import dataclasses

    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        c.host = "10.0.0.1"  # type: ignore[misc]


def test_netcontext_carries_gateway_and_runner() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(args: tuple[str, ...]) -> str:
        calls.append(args)
        return "ok"

    net = NetContext(gateway_ip="192.168.2.1", runner=runner)
    assert net.gateway_ip == "192.168.2.1"
    assert net.runner is not None
    assert net.runner(("probe",)) == "ok"
    assert calls == [("probe",)]


def test_netcontext_allows_none_runner() -> None:
    net = NetContext(gateway_ip=None, runner=None)
    assert net.gateway_ip is None
    assert net.runner is None


def test_opresult_defaults() -> None:
    r = OpResult(ok=True, detail="hi")
    assert r.before is None
    assert r.after is None


def test_snapshot_holds_data_mapping() -> None:
    data: Mapping[str, str] = {"a": "1"}
    snap = Snapshot(brand="fake", taken_at="t", data=dict(data))
    assert snap.data["a"] == "1"
    assert snap.brand == "fake"


def test_device_error_is_local_error() -> None:
    from sanctum_cli.errors import LocalError

    err = DeviceError("boom")
    assert isinstance(err, LocalError)
    assert err.exit_code == LocalError.exit_code
