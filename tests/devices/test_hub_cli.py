"""``sanctum net hub`` CLI surface — driven against an in-memory FakeProvider.

Task 6 wires the Layer-1 provider + Layer-2 intent under a Typer ``hub`` sub-app
of ``net``. These tests exercise that surface end-to-end through Typer's
``CliRunner`` while pointing the registry at a :class:`FakeProvider` (monkeypatched
``registry.resolve``) and stubbing credential resolution — so no network, no
Keychain, no live hub is ever touched. The single-NAT command must default to a
dry-run that mutates NOTHING (the cutover is attended-only and out of scope).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.devices.base import Capability, CapabilityOp, OpResult, Snapshot
from sanctum_cli.devices.intents import BRIDGE_MODE_PATH

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


class FakeProvider:
    """Minimal in-memory hub the CLI drives in place of a real Sagemcom hub.

    Tracks ``set``/``rollback``/``connect`` calls so the tests can assert the
    dry-run path mutates nothing and the apply path routes through the rails.
    """

    kind = "hub"
    brand = "fake-hub"

    def __init__(self) -> None:
        self._v: dict[str, str] = {
            BRIDGE_MODE_PATH: "off",
            "Device/DeviceInfo/ModelName": "F@st-5697",
            "Device/DeviceInfo/SoftwareVersion": "1.2.3",
        }
        self.set_calls: list[tuple[str, str]] = []
        self.rollback_calls = 0
        self.connected = False
        self.disconnected = False

    @staticmethod
    def detect(net: object) -> float:
        return 1.0

    def connect(self, creds: object | None) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def get(self, path: str) -> str | None:
        return self._v.get(path)

    def set(self, path: str, value: str) -> OpResult:
        self.set_calls.append((path, value))
        before = self._v.get(path)
        self._v[path] = value
        return OpResult(ok=True, detail="set", before=before, after=value)

    def capabilities(self) -> set[Capability]:
        return {Capability.READ, Capability.SET, Capability.BRIDGE_MODE}

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        if capability is Capability.BRIDGE_MODE:
            return CapabilityOp(path=BRIDGE_MODE_PATH, engaged="on")
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(brand=self.brand, taken_at="t", data=dict(self._v))

    def rollback(self, snap: Snapshot) -> OpResult:
        self.rollback_calls += 1
        self._v = dict(snap.data)
        return OpResult(ok=True, detail="rolled back")


def _point_registry_at(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    """Make ``net hub`` resolve to ``provider`` and never touch creds/network."""
    monkeypatch.setattr(
        "sanctum_cli.commands.net.registry.resolve",
        lambda _kind, _net, brand_pin=None: provider,
    )
    # Build NetContext without shelling out to `route`, and creds without Keychain.
    monkeypatch.setattr(
        "sanctum_cli.commands.net._hub_netcontext",
        lambda: __import__(
            "sanctum_cli.devices.base", fromlist=["NetContext"]
        ).NetContext(gateway_ip="192.168.2.1", runner=None),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._hub_creds",
        lambda net: __import__("sanctum_cli.devices.base", fromlist=["Creds"]).Creds(
            host="192.168.2.1", username="admin", secret=None, key_path=None
        ),
    )


def test_net_hub_status_prints_model_firmware_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """`net hub status` is read-only and reports model + firmware + bridge-mode."""
    p = FakeProvider()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "hub", "status"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "f@st-5697" in out  # model
    assert "1.2.3" in out  # firmware
    assert "off" in out  # bridge-mode value
    assert p.connected is True
    assert p.set_calls == []  # status never mutates


def test_net_hub_get_prints_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """`net hub get <path>` prints the single leaf value."""
    p = FakeProvider()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "hub", "get", BRIDGE_MODE_PATH])
    assert result.exit_code == 0, result.stdout
    assert "off" in result.stdout
    assert p.set_calls == []


def test_net_hub_set_force_mutates(monkeypatch: pytest.MonkeyPatch) -> None:
    """`net hub set <path> <val> --force` flips the leaf through the rails."""
    p = FakeProvider()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(
        app, ["net", "hub", "set", BRIDGE_MODE_PATH, "on", "--force"]
    )
    assert result.exit_code == 0, result.stdout
    assert (BRIDGE_MODE_PATH, "on") in p.set_calls
    assert p.get(BRIDGE_MODE_PATH) == "on"


def test_net_hub_single_nat_dryrun_mutates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`net hub single-nat` is now a DEPRECATION shim: it steers to the staged
    `net single-nat` command and fires ZERO mutations — never the old SetBridgeMode
    leaf (the proven-dead/hub-capped path).
    """
    p = FakeProvider()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "hub", "single-nat"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "deprecated" in out  # steers the operator to the new command
    assert "net single-nat" in out
    # The hard guardrail (unchanged): a deprecated dry-run fires ZERO mutations.
    assert p.set_calls == []
    assert p.rollback_calls == 0
    assert p.get(BRIDGE_MODE_PATH) == "off"


def test_net_hub_brand_pin_threaded_from_instance_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instance.yaml devices.hub.brand pin reaches registry.resolve(brand_pin=).

    This is the escape hatch that lets the real Bell hub resolve end-to-end even
    though the Sagemcom read-only probe is a stub (detect()→0). We assert the pin
    configured in instance.yaml is the brand_pin handed to resolve.
    """
    p = FakeProvider()
    # Build creds/netcontext without shelling out (mirrors _point_registry_at),
    # but spy on resolve to capture the brand_pin kwarg.
    monkeypatch.setattr(
        "sanctum_cli.commands.net._hub_netcontext",
        lambda: __import__(
            "sanctum_cli.devices.base", fromlist=["NetContext"]
        ).NetContext(gateway_ip="192.168.2.1", runner=None),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._hub_creds",
        lambda net: __import__("sanctum_cli.devices.base", fromlist=["Creds"]).Creds(
            host="192.168.2.1", username="admin", secret=None, key_path=None
        ),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net.config.instance_value",
        lambda key, default=None: "sagemcom" if key == "devices.hub.brand" else default,
    )
    captured: dict[str, object] = {}

    def spy_resolve(kind: str, net: object, *, brand_pin: object = None) -> object:
        captured["brand_pin"] = brand_pin
        return p

    monkeypatch.setattr("sanctum_cli.commands.net.registry.resolve", spy_resolve)

    result = runner.invoke(app, ["net", "hub", "status"])
    assert result.exit_code == 0, result.stdout
    assert captured["brand_pin"] == "sagemcom"


def test_net_hub_status_disconnects_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hub command must release the provider via disconnect() (no loop leak)."""
    p = FakeProvider()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "hub", "status"])
    assert result.exit_code == 0, result.stdout
    assert p.connected is True
    assert p.disconnected is True  # the lifecycle teardown ran


def test_net_hub_disconnects_even_when_command_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect() must fire in a finally even when the command body raises."""
    from sanctum_cli.devices.base import DeviceError

    class GetRaisesProvider(FakeProvider):
        def get(self, path: str) -> str | None:
            msg = "transport died mid-read"
            raise DeviceError(msg)

    p = GetRaisesProvider()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "hub", "get", BRIDGE_MODE_PATH])
    # DeviceError → clean exit code, AND the provider was still disconnected.
    assert result.exit_code == 4
    assert p.disconnected is True


def test_net_hub_single_nat_apply_is_inert_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SAFETY: the DEPRECATED `net hub single-nat --apply --force` must fire ZERO
    mutations and NEVER call the old `intents.single_nat` (SetBridgeMode) path.

    The single-leaf SetBridgeMode cutover was proven dead/hub-capped, so the shim
    must steer to `net single-nat` (the staged Advanced-DMZ + /32 orchestrator)
    rather than silently firing the old path on a deprecated --apply. The real,
    fw-bound-runner apply path is exercised by tests/commands/test_single_nat_cli.py
    against the new command.
    """
    p = FakeProvider()
    _point_registry_at(monkeypatch, p)

    # Tripwire: the deprecated command must NOT reach the old single_nat intent.
    called = {"old_intent": False}

    def boom_single_nat(*_a: object, **_k: object) -> object:
        called["old_intent"] = True
        msg = "deprecated shim must NOT call the old SetBridgeMode intent"
        raise AssertionError(msg)

    monkeypatch.setattr("sanctum_cli.commands.net.intents.single_nat", boom_single_nat)

    result = runner.invoke(app, ["net", "hub", "single-nat", "--apply", "--force"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "deprecated" in out
    assert "net single-nat" in out
    # Inert: zero mutations even with --apply --force, and the old intent untouched.
    assert called["old_intent"] is False
    assert p.set_calls == []
    assert p.rollback_calls == 0


def test_net_hub_set_unsupported_is_legible(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider that refuses set (generic read-only) surfaces a clean error code."""
    from sanctum_cli.devices.base import DeviceError

    class RefusingProvider(FakeProvider):
        def snapshot(self, scope: str | None = None) -> Snapshot:
            raise DeviceError("read-only: no provider for hub; contribute one")

    p = RefusingProvider()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(
        app, ["net", "hub", "set", BRIDGE_MODE_PATH, "on", "--force"]
    )
    # DeviceError is a LocalError → exit code 4, not an unhandled traceback.
    assert result.exit_code == 4
