"""``sanctum net firewalla`` CLI surface — driven against an in-memory FakeProvider.

Task 4 wires the Layer-1 Firewalla provider under a Typer ``firewalla`` sub-app of
``net`` (mirroring the ``hub`` sub-app). These tests exercise that surface
end-to-end through Typer's ``CliRunner`` while pointing the registry at a
:class:`FakeFirewalla` (monkeypatched ``registry.resolve``) and stubbing
credential/context resolution — so no network, no bridge HTTP, no SSH, no live
Firewalla (10.0.0.1 / firewalla.local) is ever touched.

SAFETY: the ``pause`` command is MUTATING and must default to a **dry-run** that
fires ZERO ``set`` calls; the cutover to a paused policy is opt-in (``--apply``)
and routes through the ``guarded_apply`` rails (snapshot → confirm → verify →
rollback). The overnight build never mutates live gear.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.devices.base import Capability, CapabilityOp, OpResult, Snapshot

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


class FakeFirewalla:
    """Minimal in-memory Firewalla the CLI drives in place of a real box.

    Tracks ``set``/``rollback``/``connect`` calls so tests can assert the dry-run
    path mutates nothing and the apply path routes through the rails. ``get``
    returns canned JSON-string bodies for the read endpoints the CLI surfaces.
    """

    kind = "firewalla"
    brand = "firewalla-gold"

    def __init__(self) -> None:
        self._bodies: dict[str, str] = {
            "/info": '{"box":{"model":"gold","version":"1.975"}}',
            "/policies": '{"policies":[{"pid":"7","paused":false}],"count":1}',
            "/flows": '{"flows":[{"src":"10.0.0.5","dst":"1.1.1.1"}],"count":1}',
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
        return self._bodies.get(path)

    def set(self, path: str, value: str) -> OpResult:
        self.set_calls.append((path, value))
        return OpResult(ok=True, detail="set", before=None, after=value)

    def capabilities(self) -> set[Capability]:
        # Honest surface: no WAN_MODE (the bridge proxies no WAN-mode route),
        # mirroring the real FirewallaProvider.
        return {
            Capability.READ,
            Capability.POLICY,
            Capability.SCREEN_TIME,
        }

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(
            brand=self.brand,
            taken_at="t",
            data={"/policies": self._bodies["/policies"]},
        )

    def rollback(self, snap: Snapshot) -> OpResult:
        self.rollback_calls += 1
        return OpResult(ok=True, detail="rolled back")


def _point_registry_at(monkeypatch: pytest.MonkeyPatch, provider: FakeFirewalla) -> None:
    """Make ``net firewalla`` resolve to ``provider`` and never touch creds/network."""
    monkeypatch.setattr(
        "sanctum_cli.commands.net.registry.resolve",
        lambda _kind, _net, brand_pin=None: provider,
    )
    # Build NetContext without shelling out to `route`, and creds without the bridge.
    monkeypatch.setattr(
        "sanctum_cli.commands.net._firewalla_netcontext",
        lambda: __import__(
            "sanctum_cli.devices.base", fromlist=["NetContext"]
        ).NetContext(gateway_ip="10.0.0.1", runner=None),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._firewalla_creds",
        lambda net: __import__("sanctum_cli.devices.base", fromlist=["Creds"]).Creds(
            host="firewalla.local", username="pi", secret=None, key_path=None
        ),
    )


# ── status (read-only) ────────────────────────────────────────────────


def test_net_firewalla_status_prints_brand_and_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`net firewalla status` is read-only and reports brand + a box summary."""
    p = FakeFirewalla()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "firewalla", "status"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "firewalla-gold" in out  # brand
    assert "firewalla" in out  # kind
    assert p.connected is True
    assert p.set_calls == []  # status never mutates


def test_net_firewalla_status_dead_bridge_exits_nonzero_not_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead / unauthorized bridge must exit nonzero, NOT print 'info: -' exit 0.

    The real FirewallaProvider.get now RAISES DeviceError on transport-down /
    auth-reject (the Protocol contract Sagemcom already honors), instead of
    swallowing it into None. firewalla_status catches SanctumError and exits the
    LOCAL_ERROR code (4) — so a total connectivity / auth failure is reported as a
    failure, not disguised as 'box up, empty body' with exit 0.
    """
    from sanctum_cli.devices.base import DeviceError

    class DeadBridgeFirewalla(FakeFirewalla):
        def get(self, path: str) -> str | None:
            msg = "Firewalla bridge unreachable for GET '/info'"
            raise DeviceError(msg, fix="check the bridge is up")

    p = DeadBridgeFirewalla()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "firewalla", "status"])
    assert result.exit_code == 4, result.stdout  # LOCAL_ERROR, not a silent 0
    assert p.disconnected is True  # still released the provider


def test_net_firewalla_status_disconnects_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every firewalla command must release the provider via disconnect()."""
    p = FakeFirewalla()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "firewalla", "status"])
    assert result.exit_code == 0, result.stdout
    assert p.disconnected is True  # the lifecycle teardown ran


# ── policies (read) ────────────────────────────────────────────────────


def test_net_firewalla_policies_prints_policy_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`net firewalla policies` prints the read-only policy state."""
    p = FakeFirewalla()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "firewalla", "policies"])
    assert result.exit_code == 0, result.stdout
    assert "pid" in result.stdout  # the policy payload was surfaced
    assert p.set_calls == []  # read never mutates


# ── flows (read) ───────────────────────────────────────────────────────


def test_net_firewalla_flows_prints_flow_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`net firewalla flows` prints the read-only flow state."""
    p = FakeFirewalla()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "firewalla", "flows"])
    assert result.exit_code == 0, result.stdout
    assert "1.1.1.1" in result.stdout  # the flow payload was surfaced
    assert p.set_calls == []  # read never mutates


# ── pause (DESCOPED to read-only preview — bridge routes do not exist yet) ──
#
# The mutating apply path is descoped (Task 6 finding 2): POST /policies/<id>/pause
# (apply) and POST /policies/restore (rollback) appear NOWHERE in the established
# Firewalla bridge contract — the shipping screen_time surface only GETs /info,
# /policies, /host/<mac>, /hosts and references the one documented mutate
# /policies/purge. Against the real box both invented routes 404, so the productized
# pause/rollback path cannot succeed and emits a "ROLLBACK FAILED" message on the
# happy path. Pause is therefore a read-only PREVIEW until the routes exist + an
# env-gated contract smoke confirms their shapes. The rails-level contract for the
# return-convention adapter (finding 1) lives durably in test_rails.py for the
# future re-enable.


def test_net_firewalla_pause_preview_mutates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`net firewalla pause <target>` prints the preview plan and mutates nothing."""
    p = FakeFirewalla()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "firewalla", "pause", "7"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "7" in out  # the target policy is named in the preview plan
    # The hard guardrail: a preview fires ZERO mutations.
    assert p.set_calls == []
    assert p.rollback_calls == 0


def test_net_firewalla_pause_apply_is_descoped_and_fires_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pause --apply` is DESCOPED: it must refuse with a nonzero exit and fire no set.

    The invented bridge routes (POST /policies/<id>/pause + /policies/restore) are
    not in the bridge contract, so firing the mutation would 404 → ok=False →
    a "ROLLBACK FAILED" message on a route that also does not exist. The command
    refuses loudly (LOCAL_ERROR exit 4) instead of reaching provider.set /
    guarded_apply — the hard guardrail for the overnight build.
    """
    p = FakeFirewalla()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(
        app, ["net", "firewalla", "pause", "7", "--apply", "--force"]
    )
    assert result.exit_code == 4, result.stdout  # descoped → LOCAL_ERROR, not a fire
    # No mutation reached the provider — not even a connect for a write.
    assert p.set_calls == []
    assert p.rollback_calls == 0


def test_net_firewalla_disconnects_even_when_command_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect() must fire in a finally even when the command body raises."""
    from sanctum_cli.devices.base import DeviceError

    class GetRaisesFirewalla(FakeFirewalla):
        def get(self, path: str) -> str | None:
            msg = "bridge died mid-read"
            raise DeviceError(msg)

    p = GetRaisesFirewalla()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "firewalla", "status"])
    # DeviceError → clean exit code, AND the provider was still disconnected.
    assert result.exit_code == 4
    assert p.disconnected is True


def test_net_firewalla_brand_pin_threaded_from_instance_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instance.yaml devices.firewalla.brand pin reaches registry.resolve(brand_pin=)."""
    p = FakeFirewalla()
    monkeypatch.setattr(
        "sanctum_cli.commands.net._firewalla_netcontext",
        lambda: __import__(
            "sanctum_cli.devices.base", fromlist=["NetContext"]
        ).NetContext(gateway_ip="10.0.0.1", runner=None),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._firewalla_creds",
        lambda net: __import__("sanctum_cli.devices.base", fromlist=["Creds"]).Creds(
            host="firewalla.local", username="pi", secret=None, key_path=None
        ),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net.config.instance_value",
        lambda key, default=None: "firewalla" if key == "devices.firewalla.brand" else default,
    )
    captured: dict[str, object] = {}

    def spy_resolve(kind: str, net: object, *, brand_pin: object = None) -> object:
        captured["kind"] = kind
        captured["brand_pin"] = brand_pin
        return p

    monkeypatch.setattr("sanctum_cli.commands.net.registry.resolve", spy_resolve)

    result = runner.invoke(app, ["net", "firewalla", "status"])
    assert result.exit_code == 0, result.stdout
    assert captured["kind"] == "firewalla"
    assert captured["brand_pin"] == "firewalla"
