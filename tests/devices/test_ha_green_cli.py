"""``sanctum net ha-green`` CLI surface — driven against an in-memory FakeProvider.

Wires the Layer-1 HA Green provider under a Typer ``ha-green`` sub-app of ``net``
(mirroring the ``firewalla`` sub-app). These tests exercise that surface end-to-end
through Typer's ``CliRunner`` while pointing the registry at a :class:`FakeHaGreen`
(monkeypatched ``registry.resolve``) and stubbing context/cred resolution — so no
network, no REST HTTP, no Tailscale, no live Green (10.0.0.3) is ever touched.

HONEST-VERIFY: ``status`` reports ✓/✗ from REAL checks (LAN TCP connect, the
``/api/`` running marker, the tailnet listing) and exits ``LOCAL_ERROR`` when the
Core API is NOT up — never a dash-and-exit-0 over a dead box. The surface is
read-only, so there is no mutating command to guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.devices.base import Capability, CapabilityOp, OpResult, Snapshot

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


class FakeHaGreen:
    """Minimal in-memory HA Green the CLI drives in place of a real appliance."""

    kind = "ha-green"
    brand = "ha-green-2026.6.1"

    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False
        self.set_calls: list[tuple[str, str]] = []

    @staticmethod
    def detect(net: object) -> float:
        return 1.0

    def connect(self, creds: object | None) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def get(self, path: str) -> str | None:
        return '{"message":"API running."}' if path == "/api/" else None

    def set(self, path: str, value: str) -> OpResult:
        self.set_calls.append((path, value))
        return OpResult(ok=False, detail="read-only")

    def capabilities(self) -> set[Capability]:
        return {Capability.READ}

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(brand=self.brand, taken_at="t", data={})

    def rollback(self, snap: Snapshot) -> OpResult:
        return OpResult(ok=False, detail="read-only")


def _point_registry_at(monkeypatch: pytest.MonkeyPatch, provider: FakeHaGreen) -> None:
    """Make ``net ha-green`` resolve to ``provider`` and never touch creds/network."""
    monkeypatch.setattr(
        "sanctum_cli.commands.net.registry.resolve",
        lambda _kind, _net, brand_pin=None: provider,
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._ha_green_netcontext",
        lambda: __import__("sanctum_cli.devices.base", fromlist=["NetContext"]).NetContext(
            gateway_ip="10.0.0.1", runner=None
        ),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._ha_green_creds",
        lambda net: __import__("sanctum_cli.devices.base", fromlist=["Creds"]).Creds(
            host="10.0.0.3", username="owner", secret=None, key_path=None
        ),
    )


def _stub_health(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lan: bool,
    api: bool,
    version: str | None,
    tailnet: bool,
) -> None:
    """Stub the provider-module health seams the status command reads (no I/O)."""
    monkeypatch.setattr("sanctum_cli.devices.ha_green.lan_reachable", lambda: lan)
    monkeypatch.setattr("sanctum_cli.devices.ha_green.api_running", lambda: api)
    monkeypatch.setattr("sanctum_cli.devices.ha_green.ha_version", lambda: version)
    monkeypatch.setattr("sanctum_cli.devices.ha_green.tailscale_node_present", lambda: tailnet)


# ── status: healthy ────────────────────────────────────────────────────


def test_net_ha_green_status_healthy_reports_all_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fully healthy Green: brand + LAN ✓ + API up (version) + tailnet joined, exit 0."""
    p = FakeHaGreen()
    _point_registry_at(monkeypatch, p)
    _stub_health(monkeypatch, lan=True, api=True, version="2026.6.1", tailnet=True)
    result = runner.invoke(app, ["net", "ha-green", "status"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "ha-green-2026.6.1" in out  # brand
    assert "ha-green" in out  # kind
    assert "reachable" in out  # LAN row
    assert "up" in out and "2026.6.1" in result.stdout  # API row + version
    assert "joined" in out  # tailnet row
    assert "homeassistant" in out
    assert p.connected is True
    assert p.disconnected is True  # lifecycle teardown ran
    assert p.set_calls == []  # status never mutates


# ── status: API down → honest nonzero, not a fake ✓ ────────────────────


def test_net_ha_green_status_api_down_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable box whose Core API is NOT up exits LOCAL_ERROR, not a silent 0.

    HONEST-VERIFY: 'down' must never render the success colour or exit 0 — the
    command is a scriptable health gate. The provider is still disconnected.
    """
    p = FakeHaGreen()
    _point_registry_at(monkeypatch, p)
    _stub_health(monkeypatch, lan=True, api=False, version=None, tailnet=False)
    result = runner.invoke(app, ["net", "ha-green", "status"])
    assert result.exit_code == 4, result.stdout  # LOCAL_ERROR
    out = result.stdout.lower()
    assert "down" in out  # the honest API row
    assert p.disconnected is True


def test_net_ha_green_status_lan_unreachable_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    """LAN-unreachable + API-down: both rows read the honest failure and exit nonzero."""
    p = FakeHaGreen()
    _point_registry_at(monkeypatch, p)
    _stub_health(monkeypatch, lan=False, api=False, version=None, tailnet=False)
    result = runner.invoke(app, ["net", "ha-green", "status"])
    assert result.exit_code == 4, result.stdout
    out = result.stdout.lower()
    assert "unreachable" in out
    assert "down" in out
    assert "not joined" in out


def test_net_ha_green_status_remote_not_joined_but_local_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote access (Tailscale) absent is a calm note, not a failure — exit 0 if API up."""
    p = FakeHaGreen()
    _point_registry_at(monkeypatch, p)
    _stub_health(monkeypatch, lan=True, api=True, version="2026.6.1", tailnet=False)
    result = runner.invoke(app, ["net", "ha-green", "status"])
    assert result.exit_code == 0, result.stdout  # local health is fine; remote is additive
    assert "not joined" in result.stdout.lower()


# ── lifecycle: disconnect even when the command errors ─────────────────


def test_net_ha_green_status_disconnects_even_when_command_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect() must fire in a finally even when a health seam raises a DeviceError."""
    from sanctum_cli.devices.base import DeviceError

    p = FakeHaGreen()
    _point_registry_at(monkeypatch, p)

    def boom() -> bool:
        raise DeviceError("HA Green rejected the token", fix="use the owner token")

    monkeypatch.setattr("sanctum_cli.devices.ha_green.lan_reachable", lambda: True)
    monkeypatch.setattr("sanctum_cli.devices.ha_green.api_running", boom)
    result = runner.invoke(app, ["net", "ha-green", "status"])
    assert result.exit_code == 4, result.stdout  # DeviceError → LOCAL_ERROR
    assert p.disconnected is True  # still released the provider


# ── brand pin threaded from instance.yaml ──────────────────────────────


def test_net_ha_green_brand_pin_threaded_from_instance_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instance.yaml devices.ha-green.brand pin reaches registry.resolve(brand_pin=)."""
    p = FakeHaGreen()
    monkeypatch.setattr(
        "sanctum_cli.commands.net._ha_green_netcontext",
        lambda: __import__("sanctum_cli.devices.base", fromlist=["NetContext"]).NetContext(
            gateway_ip="10.0.0.1", runner=None
        ),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._ha_green_creds",
        lambda net: __import__("sanctum_cli.devices.base", fromlist=["Creds"]).Creds(
            host="10.0.0.3", username="owner", secret=None, key_path=None
        ),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net.config.instance_value",
        lambda key, default=None: "ha-green" if key == "devices.ha-green.brand" else default,
    )
    _stub_health(monkeypatch, lan=True, api=True, version="2026.6.1", tailnet=True)
    captured: dict[str, object] = {}

    def spy_resolve(kind: str, net: object, *, brand_pin: object = None) -> object:
        captured["kind"] = kind
        captured["brand_pin"] = brand_pin
        return p

    monkeypatch.setattr("sanctum_cli.commands.net.registry.resolve", spy_resolve)

    result = runner.invoke(app, ["net", "ha-green", "status"])
    assert result.exit_code == 0, result.stdout
    assert captured["kind"] == "ha-green"
    assert captured["brand_pin"] == "ha-green"
