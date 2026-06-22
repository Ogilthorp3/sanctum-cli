"""``sanctum net orbi`` CLI surface — driven against an in-memory FakeOrbi.

Task 2 wires the Layer-1 Orbi provider under a Typer ``orbi`` sub-app of ``net``
(mirroring the ``hub`` + ``firewalla`` sub-apps). These tests exercise that
surface end-to-end through Typer's ``CliRunner`` while pointing the registry at a
:class:`FakeOrbi` (monkeypatched ``registry.resolve``) and stubbing
credential/context resolution — so no network, no SOAP, no pynetgear, no live
Orbi (192.168.1.1) is ever touched.

SAFETY: the ``guest-wifi`` command is MUTATING and must default to a **dry-run**
that fires ZERO ``set`` calls; the flip is opt-in (``--apply``) and routes
through the ``guarded_apply`` rails (snapshot → confirm → verify → rollback) via
the provider's ``GUEST_WIFI`` capability_op. The overnight build never mutates
live gear.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.devices.base import Capability, CapabilityOp, OpResult, Snapshot

if TYPE_CHECKING:
    import pytest

runner = CliRunner()

GUEST_2G = "guest_wifi/2g"
GUEST_5G = "guest_wifi/5g"
CHANNEL_2G = "channel/2g"
CHANNEL_5G = "channel/5g"
INFO_MODEL = "info/model"
FIRMWARE = "firmware/new"


class FakeOrbi:
    """Minimal in-memory Orbi the CLI drives in place of a real mesh router.

    Tracks ``set``/``rollback``/``connect`` calls so tests can assert the dry-run
    path mutates nothing and the apply path routes through the rails. ``get``
    returns canned string values for the read leaves the CLI surfaces, and
    ``capability_op(GUEST_WIFI)`` returns the (path, engaged) the apply path uses.
    """

    kind = "orbi"
    brand = "orbi-rbr50"

    def __init__(self) -> None:
        self._values: dict[str, str] = {
            GUEST_2G: "off",
            GUEST_5G: "off",
            CHANNEL_2G: "6",
            CHANNEL_5G: "44",
            INFO_MODEL: "RBR50",
            FIRMWARE: "",
        }
        self.set_calls: list[tuple[str, str]] = []
        self.rollback_calls = 0
        self.connected = False
        self.disconnected = False
        self.verify_result = True

    @staticmethod
    def detect(net: object) -> float:
        return 1.0

    def connect(self, creds: object | None) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def get(self, path: str) -> str | None:
        return self._values.get(path)

    def set(self, path: str, value: str) -> OpResult:
        self.set_calls.append((path, value))
        before = self._values.get(path)
        self._values[path] = value
        return OpResult(ok=True, detail=f"set {path}", before=before, after=value)

    def capabilities(self) -> set[Capability]:
        return {
            Capability.READ,
            Capability.FIRMWARE,
            Capability.AP_MODE,
            Capability.CHANNELS,
            Capability.GUEST_WIFI,
        }

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        if capability is Capability.GUEST_WIFI:
            return CapabilityOp(path=GUEST_5G, engaged="on")
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(
            brand=self.brand,
            taken_at="t",
            data={GUEST_2G: self._values[GUEST_2G], GUEST_5G: self._values[GUEST_5G]},
        )

    def rollback(self, snap: Snapshot) -> OpResult:
        self.rollback_calls += 1
        return OpResult(ok=True, detail="rolled back")


def _point_registry_at(monkeypatch: pytest.MonkeyPatch, provider: FakeOrbi) -> None:
    """Make ``net orbi`` resolve to ``provider`` and never touch creds/network."""
    monkeypatch.setattr(
        "sanctum_cli.commands.net.registry.resolve",
        lambda _kind, _net, brand_pin=None: provider,
    )
    # Build NetContext without shelling out to `route`, and creds without a SOAP login.
    monkeypatch.setattr(
        "sanctum_cli.commands.net._orbi_netcontext",
        lambda: __import__(
            "sanctum_cli.devices.base", fromlist=["NetContext"]
        ).NetContext(gateway_ip="192.168.1.1", runner=None),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._orbi_creds",
        lambda net: __import__("sanctum_cli.devices.base", fromlist=["Creds"]).Creds(
            host="192.168.1.1", username="admin", secret=None, key_path=None
        ),
    )


# ── status (read-only) ────────────────────────────────────────────────


def test_net_orbi_status_prints_brand_and_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`net orbi status` is read-only and reports brand + a guest-wifi summary."""
    p = FakeOrbi()
    p._values[GUEST_5G] = "on"
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "orbi", "status"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "orbi-rbr50" in out  # brand
    assert "orbi" in out  # kind
    assert "on" in out  # guest-wifi 5g state surfaced
    assert p.connected is True
    assert p.set_calls == []  # status never mutates


def test_net_orbi_status_dead_transport_exits_nonzero_not_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead / un-authable SOAP transport must exit nonzero, NOT print a dash exit 0.

    The real OrbiProvider.get RAISES DeviceError on a transport failure (the
    Protocol contract Sagemcom/Firewalla already honor). orbi_status catches
    SanctumError and exits the LOCAL_ERROR code (4) — so a total connectivity /
    auth failure is reported as a failure, not disguised as 'box up, empty'.
    """
    from sanctum_cli.devices.base import DeviceError

    class DeadOrbi(FakeOrbi):
        def get(self, path: str) -> str | None:
            msg = "Orbi guest-access read failed for 5g"
            raise DeviceError(msg, fix="check the box is reachable")

    p = DeadOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "orbi", "status"])
    assert result.exit_code == 4, result.stdout  # LOCAL_ERROR, not a silent 0
    assert p.disconnected is True  # still released the provider


def test_net_orbi_status_disconnects_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every orbi command must release the provider via disconnect()."""
    p = FakeOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "orbi", "status"])
    assert result.exit_code == 0, result.stdout
    assert p.disconnected is True  # the lifecycle teardown ran


# ── firmware (read) ────────────────────────────────────────────────────


def test_net_orbi_firmware_prints_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """`net orbi firmware` prints the read-only firmware state and mutates nothing."""
    p = FakeOrbi()
    p._values[FIRMWARE] = "V2.7.4"
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "orbi", "firmware"])
    assert result.exit_code == 0, result.stdout
    assert "V2.7.4" in result.stdout  # the firmware payload was surfaced
    assert p.set_calls == []  # read never mutates


# ── channels (read) ────────────────────────────────────────────────────


def test_net_orbi_channels_prints_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """`net orbi channels` prints the read-only 2g/5g channel state."""
    p = FakeOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "orbi", "channels"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "6" in out  # 2g channel
    assert "44" in out  # 5g channel
    assert p.set_calls == []  # read never mutates


# ── guest-wifi (mutating → dry-run default → guarded_apply on --apply) ──


def test_net_orbi_guest_wifi_dry_run_mutates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`net orbi guest-wifi on` (no --apply) prints the plan and fires ZERO sets."""
    p = FakeOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "orbi", "guest-wifi", "on"])
    assert result.exit_code == 0, result.stdout
    # The hard guardrail: a dry-run fires ZERO mutations.
    assert p.set_calls == []
    assert p.rollback_calls == 0
    assert "dry-run" in result.stdout.lower()


def test_net_orbi_guest_wifi_apply_routes_through_rails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`guest-wifi on --apply --force` fires the set through guarded_apply and commits."""
    p = FakeOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(
        app, ["net", "orbi", "guest-wifi", "on", "--apply", "--force"]
    )
    assert result.exit_code == 0, result.stdout
    # The mutation routed through the provider's GUEST_WIFI capability_op (5g leaf).
    assert (GUEST_5G, "on") in p.set_calls
    # A passing verify commits — no rollback fired.
    assert p.rollback_calls == 0


def test_net_orbi_guest_wifi_apply_set_returns_not_ok_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider whose set RETURNS ok=False (refused write) must trip rollback.

    The return-convention contract end-to-end (P2 low finding): OrbiProvider.set
    returns ok=False on an unwritable leaf instead of raising. The guest-wifi
    apply closure passes that OpResult THROUGH to the rails, which now inspect it
    and treat ok=False as a failed apply — so a refused write is NOT reported as a
    green success even though the verify (re-read) would have passed. Without the
    call-site returning the OpResult (and the rails inspecting it), this would exit
    0 and silently no-op.
    """

    class RefusingOrbi(FakeOrbi):
        def set(self, path: str, value: str) -> OpResult:
            # Mutate state (so the post-change verify re-read would PASS) but REFUSE
            # via the return convention (ok=False, no raise). This is the silent-
            # discard trap: only inspecting the returned OpResult catches it — a
            # verify that re-reads the (now-correct-looking) leaf would commit.
            self.set_calls.append((path, value))
            before = self._values.get(path)
            self._values[path] = value
            return OpResult(ok=False, detail="orbi: write refused upstream", before=before, after=None)

    p = RefusingOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(
        app, ["net", "orbi", "guest-wifi", "on", "--apply", "--force"]
    )
    assert result.exit_code == 1, result.stdout  # refused write → nonzero, NOT 0
    assert p.rollback_calls == 1  # the refused write was rolled back


def test_net_orbi_guest_wifi_off_uses_disengaged_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`guest-wifi off --apply --force` writes the OFF value to the capability leaf."""
    p = FakeOrbi()
    p._values[GUEST_5G] = "on"
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(
        app, ["net", "orbi", "guest-wifi", "off", "--apply", "--force"]
    )
    assert result.exit_code == 0, result.stdout
    assert (GUEST_5G, "off") in p.set_calls


def test_net_orbi_guest_wifi_rejects_bad_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid <on|off> argument is rejected before any provider work."""
    p = FakeOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "orbi", "guest-wifi", "maybe"])
    assert result.exit_code != 0
    assert p.set_calls == []


def test_net_orbi_guest_wifi_apply_verify_fail_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed verify on apply trips rollback and exits nonzero.

    The CLI wires a verify into guarded_apply; when the post-change re-read does
    not reflect the requested state, the rails roll back and report ok=False.
    """

    class NoApplyOrbi(FakeOrbi):
        def set(self, path: str, value: str) -> OpResult:
            # Record the call but DON'T actually change state, so the CLI's
            # post-change verify re-read sees the old value and fails.
            self.set_calls.append((path, value))
            return OpResult(ok=True, detail="set", before="off", after=value)

    p = NoApplyOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(
        app, ["net", "orbi", "guest-wifi", "on", "--apply", "--force"]
    )
    assert result.exit_code == 1, result.stdout  # verify failed → nonzero
    assert p.rollback_calls == 1  # the rails tripped rollback


def test_net_orbi_guest_wifi_apply_verify_readback_raises_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DeviceError from the post-change read-back trips rollback, not an escape.

    The change applies cleanly (the guest network IS flipped on the device), but
    the post-change verify re-read hits a transport/auth flake and RAISES
    DeviceError — a plausible state right after a wifi-radio change. Per the
    Protocol, provider.get() raises on transport/auth failure. guarded_apply runs
    verify_fn() unguarded, so without the CLI catching the DeviceError the
    exception would escape the rails AFTER the set — leaving the guest network
    flipped ON with NO rollback. The CLI's verify must treat a raising read-back
    as verify-failure so the rails roll back the half-applied change.
    """
    from sanctum_cli.devices.base import DeviceError

    class FlakyReadBackOrbi(FakeOrbi):
        def __init__(self) -> None:
            super().__init__()
            self._set_fired = False

        def set(self, path: str, value: str) -> OpResult:
            # The change DOES apply — the device is genuinely flipped on.
            self._set_fired = True
            return super().set(path, value)

        def get(self, path: str) -> str | None:
            # The post-change verify re-read flakes and RAISES (transport died).
            if self._set_fired:
                msg = "Orbi guest-access read failed for 5g"
                raise DeviceError(msg, fix="check the box is reachable")
            return super().get(path)

    p = FlakyReadBackOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(
        app, ["net", "orbi", "guest-wifi", "on", "--apply", "--force"]
    )
    # The set fired (change applied) but the read-back raised → rails rolled back.
    assert (GUEST_5G, "on") in p.set_calls
    assert p.rollback_calls == 1  # NOT escaped: rollback restored the device
    assert result.exit_code == 1, result.stdout  # reported as a clean failure exit


def test_net_orbi_guest_wifi_disconnects_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guest-wifi command must release the provider via disconnect()."""
    p = FakeOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "orbi", "guest-wifi", "on"])
    assert result.exit_code == 0, result.stdout
    assert p.disconnected is True


# ── lifecycle + brand-pin threading ────────────────────────────────────


def test_net_orbi_disconnects_even_when_command_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect() must fire in a finally even when the command body raises."""
    from sanctum_cli.devices.base import DeviceError

    class GetRaisesOrbi(FakeOrbi):
        def get(self, path: str) -> str | None:
            msg = "SOAP died mid-read"
            raise DeviceError(msg)

    p = GetRaisesOrbi()
    _point_registry_at(monkeypatch, p)
    result = runner.invoke(app, ["net", "orbi", "status"])
    assert result.exit_code == 4
    assert p.disconnected is True


def test_net_orbi_brand_pin_threaded_from_instance_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instance.yaml devices.orbi.brand pin reaches registry.resolve(brand_pin=)."""
    p = FakeOrbi()
    monkeypatch.setattr(
        "sanctum_cli.commands.net._orbi_netcontext",
        lambda: __import__(
            "sanctum_cli.devices.base", fromlist=["NetContext"]
        ).NetContext(gateway_ip="192.168.1.1", runner=None),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._orbi_creds",
        lambda net: __import__("sanctum_cli.devices.base", fromlist=["Creds"]).Creds(
            host="192.168.1.1", username="admin", secret=None, key_path=None
        ),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net.config.instance_value",
        lambda key, default=None: "orbi" if key == "devices.orbi.brand" else default,
    )
    captured: dict[str, object] = {}

    def spy_resolve(kind: str, net: object, *, brand_pin: object = None) -> object:
        captured["kind"] = kind
        captured["brand_pin"] = brand_pin
        return p

    monkeypatch.setattr("sanctum_cli.commands.net.registry.resolve", spy_resolve)

    result = runner.invoke(app, ["net", "orbi", "status"])
    assert result.exit_code == 0, result.stdout
    assert captured["kind"] == "orbi"
    assert captured["brand_pin"] == "orbi"
