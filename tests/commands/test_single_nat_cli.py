"""``sanctum net single-nat`` CLI surface — the Advanced-DMZ cutover command.

Task 5 surfaces the staged :func:`sanctum_cli.devices.intents.single_nat_dmz`
orchestrator behind a Typer ``net single-nat`` command. These tests drive that
surface end-to-end through Typer's ``CliRunner`` while mocking every dangerous
edge — the hub provider (an in-memory :class:`FakeHub`, reused FakeSahClient-style
double), the Firewalla runner, the armor installer, and the out-of-band probe —
so no live device, no Keychain, and no SSH is ever touched.

The contract the command MUST honor:

* bare ``net single-nat`` → prints the staged plan and fires ZERO writes (dry-run).
* ``--apply`` → fires the cutover only when the out-of-band recovery probe says
  reachable; refuses (non-zero, zero writes) when the probe says unreachable.
* ``--rollback`` → drives the undo path (disable DMZ + re-lease DHCP).
* ``--force`` → skips the human confirm prompt but STILL honors the out-of-band
  probe — force is not a bypass for the recovery-path safety gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.devices.base import (
    Capability,
    CapabilityOp,
    OpResult,
    Snapshot,
)

if TYPE_CHECKING:
    import pytest

runner = CliRunner()

# The Bell Advanced-DMZ leaf the cutover engages (the brand's DMZ capability op).
DMZ_PATH = "Device/Services/BellNetworkCfg/AdvancedDMZ"


class FakeHub:
    """In-memory Sagemcom-shaped hub the CLI drives in place of a real Bell hub.

    Records set/reboot/rollback so the dry-run can be proven to make ZERO writes
    and the apply/rollback paths can be proven to route through the rails.
    """

    kind = "hub"
    brand = "fake-bell-hub"

    def __init__(self) -> None:
        self._v: dict[str, str] = {DMZ_PATH: "off"}
        self.set_calls: list[tuple[str, str]] = []
        self.reboot_calls = 0
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
        before = self._v.get(path)
        self._v[path] = value
        self.set_calls.append((path, value))
        return OpResult(ok=True, detail=f"set {path}", before=before, after=value)

    def reboot(self) -> OpResult:
        self.reboot_calls += 1
        return OpResult(ok=True, detail="reboot issued")

    def capabilities(self) -> set[Capability]:
        return {Capability.READ, Capability.SET, Capability.DMZ, Capability.REBOOT}

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        if capability is Capability.DMZ:
            return CapabilityOp(path=DMZ_PATH, engaged="on")
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        return Snapshot(brand=self.brand, taken_at="t", data=dict(self._v))

    def rollback(self, snap: Snapshot) -> OpResult:
        self.rollback_calls += 1
        self._v = dict(snap.data)
        return OpResult(ok=True, detail="rolled back DMZ")


class FakeRunner:
    """Records every Firewalla runner op; serves a scripted public lease.

    The default ``wan_ip`` is a genuinely-GLOBAL Bell address (the same one the
    net-layer ``fixtures.SINGLE_NAT`` scenario uses), NOT a documentation/test-net
    address. RFC-5737 ranges like 203.0.113.x are classified ``is_private`` by
    Python's ``ipaddress`` — so the REAL ``verify.verify`` / ``classify_nat``
    probes the CLI now wires would (correctly) treat them as double-NAT and roll
    back. The fixture must therefore model an actual public single-NAT WAN.
    """

    def __init__(self, wan_ip: str = "70.53.241.21") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.wan_ip = wan_ip

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            return self.wan_ip
        return ""


class FakeArmor:
    """Mock armor installer: records the install, never touches a host."""

    def __init__(self) -> None:
        self.installed = 0

    def install(self) -> OpResult:
        self.installed += 1
        return OpResult(ok=True, detail="armor installed")


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hub: FakeHub,
    fw: FakeRunner,
    armor: FakeArmor,
    out_of_band: bool = True,
) -> None:
    """Point ``net single-nat`` at the in-memory doubles — no network, no creds.

    Stubs hub resolution + creds (mirrors test_hub_cli._point_registry_at), the
    Firewalla-bound runner, the armor installer seam, and the out-of-band probe.
    """
    monkeypatch.setattr(
        "sanctum_cli.commands.net.registry.resolve",
        lambda _kind, _net, brand_pin=None: hub,
    )
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
    monkeypatch.setattr("sanctum_cli.commands.net._build_runner", lambda: fw)
    monkeypatch.setattr(
        "sanctum_cli.commands.net._build_armor_installer", lambda: armor
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net._out_of_band_reachable", lambda: out_of_band
    )


# ── bare command: dry-run, ZERO writes ───────────────────────────────────────


def test_net_single_nat_dry_run_prints_plan_zero_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`net single-nat` with no flags prints the staged plan and mutates NOTHING."""
    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor)
    result = runner.invoke(app, ["net", "single-nat"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "dmz" in out  # the staged plan is described
    assert "armor" in out
    # The hard guardrail: a dry-run fires ZERO mutations anywhere.
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert hub.rollback_calls == 0
    assert armor.installed == 0
    assert fw.calls == []
    assert hub.get(DMZ_PATH) == "off"


# ── --apply: gated on the out-of-band recovery probe ──────────────────────────


def test_net_single_nat_apply_fires_when_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply` with the out-of-band probe reachable drives the staged cutover."""
    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor, out_of_band=True)
    result = runner.invoke(app, ["net", "single-nat", "--apply", "--force"])
    assert result.exit_code == 0, result.stdout
    # The flip engaged Advanced DMZ via the provider's own capability op, rebooted,
    # and installed the armor kit.
    assert (DMZ_PATH, "on") in hub.set_calls
    assert hub.reboot_calls == 1
    assert armor.installed == 1
    assert hub.rollback_calls == 0  # a verified flip never rolls back


def test_net_single_nat_apply_refuses_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply` REFUSES (non-zero, zero writes) when out-of-band is unreachable.

    The cutover drops the WAN; without an out-of-band recovery path a misstep
    could strand the household dark with no way back in (flip.gate_ok)."""
    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor, out_of_band=False)
    result = runner.invoke(app, ["net", "single-nat", "--apply", "--force"])
    assert result.exit_code != 0
    out = result.stdout.lower()
    assert "out-of-band" in out or "out of band" in out
    # Refused BEFORE any mutation: zero writes anywhere.
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert armor.installed == 0


def test_net_single_nat_force_still_honors_out_of_band_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--force` skips the human confirm but does NOT bypass the recovery gate.

    Force only waives the "are you at the box" prompt; it must still refuse when
    the out-of-band probe says there is no recovery path."""
    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor, out_of_band=False)
    result = runner.invoke(app, ["net", "single-nat", "--apply", "--force"])
    assert result.exit_code != 0
    assert hub.set_calls == []  # the gate held despite --force


def test_net_single_nat_apply_without_force_declined_confirm_zero_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply` without `--force` prompts; declining the confirm fires nothing."""
    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor, out_of_band=True)
    # Decline the "are you at the box" confirmation.
    result = runner.invoke(app, ["net", "single-nat", "--apply"], input="n\n")
    assert result.exit_code != 0
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert armor.installed == 0


# ── --apply: the REAL per-stage verify gates the commit (honest-verify) ───────


def test_net_single_nat_apply_apipa_lease_rolls_back_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply` with the downstream serving a 169.254.x (APIPA) lease MUST roll
    back and exit non-zero — the cutover never reaches a real public WAN.

    This is the honest-verify contract: the CLI must wire the SAME real probe
    (``sanctum_cli.net.verify.verify`` over the runner) that ``net optimize`` and
    the old ``single_nat`` used as the per-stage ``verify`` gate. With it wired,
    an APIPA lease is ``Verdict.APIPA_ROLLBACK`` (NOT ``VERIFIED``) so the
    ``verify`` stage's probe returns falsey, the rails unwind (disable DMZ →
    re-lease DHCP), and the command exits non-zero.

    WITHOUT it wired (today's bug), the ``verify`` stage falls through to
    ``intents._default_stage_verifier`` (unconditional ``True``) and the cutover
    COMMITS on a dead APIPA WAN — fail-to-DARK. This test fails today for exactly
    that reason: no rollback, exit 0.

    The runner serves the APIPA address for BOTH ``lease_observe`` and
    ``fw_wan_ip`` — the latter is what ``verify.verify`` actually reads — so the
    expectation is derived from the real ``verify.verify`` contract, not from a
    convenient stub that shares the producer's assumption.
    """
    hub, fw, armor = FakeHub(), FakeRunner(wan_ip="169.254.10.4"), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor, out_of_band=True)
    result = runner.invoke(app, ["net", "single-nat", "--apply", "--force"])
    # The cutover did NOT succeed on a dead APIPA WAN.
    assert result.exit_code != 0, result.stdout
    # The rails unwound: DMZ disabled (provider.rollback) and re-leased DHCP.
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "off"
    assert any(tag and "release" in tag[0] for tag in fw.calls)


# ── --rollback: drives the undo path ──────────────────────────────────────────


def test_net_single_nat_rollback_calls_rollback_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--rollback` undoes the cutover: disables DMZ and re-leases DHCP."""
    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    # Pretend the hub is currently in DMZ (a prior cutover); rollback returns it.
    hub._v[DMZ_PATH] = "on"
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor)
    result = runner.invoke(app, ["net", "single-nat", "--rollback", "--force"])
    assert result.exit_code == 0, result.stdout
    # The provider's rollback ran (DMZ disabled) ...
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "off"
    # ... and the downstream DHCP re-lease fired so the WAN recovers.
    assert any(tag and "release" in tag[0] for tag in fw.calls)
    # A rollback fires no DMZ-engage / armor install.
    assert (DMZ_PATH, "on") not in hub.set_calls
    assert armor.installed == 0


# ── deprecation: old SetBridgeMode hub single-nat redirects ───────────────────


def test_hub_single_nat_emits_deprecation_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old `net hub single-nat` (SetBridgeMode) points at the new command."""
    fw, armor = FakeRunner(), FakeArmor()

    class BridgeHub(FakeHub):
        def capability_op(self, capability: Capability) -> CapabilityOp | None:
            if capability is Capability.BRIDGE_MODE:
                return CapabilityOp(
                    path="Device/Services/BellNetworkCfg/SetBridgeMode", engaged="on"
                )
            return None

    bridge_hub = BridgeHub()
    _wire(monkeypatch, hub=bridge_hub, fw=fw, armor=armor)
    result = runner.invoke(app, ["net", "hub", "single-nat"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    # The deprecation note steers the operator to the new Advanced-DMZ command.
    assert "deprecat" in out
    assert "single-nat" in out
