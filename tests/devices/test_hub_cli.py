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
from sanctum_cli.devices.base import Capability, OpResult, Snapshot
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
    """`net hub single-nat` with no --apply prints the plan and changes nothing."""
    p = FakeProvider()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "hub", "single-nat"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert BRIDGE_MODE_PATH in out  # the planned change is described
    assert "1492" in out  # MTU caveat surfaced
    # The hard guardrail: a dry-run fires ZERO mutations.
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


def test_net_hub_single_nat_apply_threads_fw_bound_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`single-nat --apply` must verify over the Firewalla-key-bound runner.

    Regression: the apply path used to pass the bare ``system.real_runner``,
    which returns "" for the ("fw_wan_ip",)/("fw_wan_mac",) tags — so
    ``verify.verify`` always saw a None WAN IP and could never fire the
    APIPA/DHCP-fail auto-rollback. We assert the runner actually handed to
    ``intents.single_nat`` resolves ("fw_wan_ip",) to the fw-bound value, i.e.
    it is ``_build_runner()`` and not the bare runner.
    """
    p = FakeProvider()
    _point_registry_at(monkeypatch, p)

    # _build_runner() shells out to `route`/SSH in production; replace it with a
    # runner that resolves the fw tags (what make_real_runner does on real gear).
    def fake_build_runner() -> object:
        def runner_fn(tag: tuple[str, ...]) -> str:
            if tag == ("fw_wan_ip",):
                return "169.254.1.5"  # APIPA → the rollback trigger MUST see it
            return ""

        return runner_fn

    monkeypatch.setattr("sanctum_cli.commands.net._build_runner", fake_build_runner)

    captured: dict[str, object] = {}
    real_single_nat = __import__(
        "sanctum_cli.devices.intents", fromlist=["single_nat"]
    ).single_nat

    def spy_single_nat(provider: object, **kwargs: object) -> object:
        captured["runner"] = kwargs.get("runner")
        return real_single_nat(provider, **kwargs)

    monkeypatch.setattr("sanctum_cli.commands.net.intents.single_nat", spy_single_nat)

    result = runner.invoke(app, ["net", "hub", "single-nat", "--apply", "--force"])
    # The runner threaded into single_nat must resolve the fw WAN IP tag — proof
    # it is the fw-key-bound runner, not the bare system.real_runner (which
    # would return "" and silently disable the APIPA rollback signal).
    threaded = captured["runner"]
    assert callable(threaded)
    assert threaded(("fw_wan_ip",)) == "169.254.1.5"
    # With an APIPA WAN, verify must trip the rollback (the cutover is undone).
    assert result.exit_code == 1, result.stdout
    assert p.rollback_calls == 1


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
