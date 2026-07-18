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

import pytest
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.devices.base import (
    Capability,
    CapabilityOp,
    OpResult,
    Snapshot,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fast_settle_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the observe_lease settle window (FIX a) to a handful of real ms ticks so
    the CLI apply tests exercise the REAL bounded poll instantly. A public lease
    settles on the first read (no sleep); an APIPA lease that never clears hard-fails
    in ~50 ms instead of the real 6-minute window. No time-mocking — real monotonic
    clock + real ``time.sleep`` on a tiny interval."""
    monkeypatch.setattr("sanctum_cli.devices.intents._SETTLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr("sanctum_cli.devices.intents._SETTLE_POLL_INTERVAL_S", 0.005)

# The Bell single-NAT leaves: the bridge-mode leaf the old ``single_nat`` flipped
# AND the Advanced-DMZ leaf the cutover engages. BOTH are single-NAT-mutating
# leaves the real provider lists in ``_MUTATED_XPATHS`` — a rollback that restores
# the CAPTURED pre-cutover baseline disengages BOTH, not just the one DMZ leaf.
BRIDGE_PATH = "Device/Services/BellNetworkCfg/SetBridgeMode"
DMZ_PATH = "Device/Services/BellNetworkCfg/AdvancedDMZ"


class FakeHub:
    """In-memory Sagemcom-shaped hub the CLI drives in place of a real Bell hub.

    Records set/reboot/rollback so the dry-run can be proven to make ZERO writes
    and the apply/rollback paths can be proven to route through the rails. It
    advertises BOTH the bridge-mode and the Advanced-DMZ capability ops (the real
    Sagemcom provider does) so a rollback that restores the captured pre-cutover
    snapshot can be proven to disengage both single-NAT leaves, not just DMZ.

    ``rollback`` re-issues a ``set`` per captured leaf (driving the recorded write
    path), so "rollback drove DMZ off" is proven from a real restore — not a dict
    swap that would silently lose an omitted key.
    """

    kind = "hub"
    brand = "fake-bell-hub"

    def __init__(self) -> None:
        self._v: dict[str, str] = {DMZ_PATH: "off"}
        self.set_calls: list[tuple[str, str]] = []
        self.reboot_calls = 0
        self.rollback_calls = 0
        self.snapshots: list[Snapshot] = []
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
        return {
            Capability.READ,
            Capability.SET,
            Capability.BRIDGE_MODE,
            Capability.DMZ,
            Capability.REBOOT,
        }

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        if capability is Capability.DMZ:
            return CapabilityOp(path=DMZ_PATH, engaged="on")
        if capability is Capability.BRIDGE_MODE:
            return CapabilityOp(path=BRIDGE_PATH, engaged="on")
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        snap = Snapshot(brand=self.brand, taken_at="t", data=dict(self._v))
        self.snapshots.append(snap)
        return snap

    def rollback(self, snap: Snapshot) -> OpResult:
        self.rollback_calls += 1
        if not snap.data:
            return OpResult(ok=False, detail="rollback failed: no restorable baseline")
        for path, value in snap.data.items():
            self.set(path, value)
        return OpResult(ok=True, detail=f"rolled back {len(snap.data)} key(s)")


class FakeRunner:
    """Records every Firewalla runner op; serves a scripted lease.

    The default ``wan_ip`` is a genuinely-GLOBAL Bell address (the same one the
    net-layer ``fixtures.SINGLE_NAT`` scenario uses), NOT a documentation/test-net
    address. RFC-5737 ranges like 203.0.113.x are classified ``is_private`` by
    Python's ``ipaddress`` — so the REAL ``verify.verify`` / ``classify_nat``
    probes the CLI wires would (correctly) treat them as double-NAT. The apply
    path wants a public single-NAT WAN; the rollback path wants the RECOVERED
    double-NAT (private) WAN — a test scripts the right one.
    """

    def __init__(self, wan_ip: str = "70.53.241.21") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.wan_ip = wan_ip

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            return self.wan_ip
        # FIX (c): a 'public' lease must also carry the WHOLE contract the real box
        # serves — the /32 armor holding + a clean route table — or the poison gate
        # (correctly) refuses to commit. Healthy armored readback by default.
        if tag == ("wan_addr_cidr",):
            return f"2: eth0    inet {self.wan_ip}/32 brd {self.wan_ip} scope global eth0"
        if tag == ("wan_routes",):
            return "default via 192.168.2.1 dev eth0"  # no 0.0.0.0/1 poison route
        return ""


class FakeArmor:
    """Mock armor installer: records stage + install, never touches a host."""

    def __init__(self) -> None:
        self.installed = 0
        self.staged = 0

    def stage(self) -> OpResult:
        # Pre-DMZ armor staging (FIX-2): the /32 hook lands before DMZ engages.
        self.staged += 1
        return OpResult(ok=True, detail="armor staged")

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
    # Live-fire config gate ARMED for these tests (the gate itself is covered by a
    # dedicated test below); the real default is OFF.
    monkeypatch.setattr(
        "sanctum_cli.commands.net._single_nat_live_armed", lambda: True
    )
    # FIX-f: the pre-apply box gate (passwordless sudo + dhclient over SSH) is a real
    # external probe; stub it READY so these tests exercise their intended paths. The
    # gate itself is covered by dedicated tests below.
    from sanctum_cli.devices import flip as _flip

    monkeypatch.setattr(
        "sanctum_cli.commands.net._box_preflight_ready",
        lambda: _flip.PreflightDecision(ok=True, reason="stubbed ready"),
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
    """`--rollback` undoes the cutover: disables DMZ, reboots to latch, re-leases
    DHCP, and verifies the WAN recovered to a working double-NAT lease.

    The runner serves a RECOVERED double-NAT (private) lease — the real post-
    rollback state once DMZ is off and the WAN re-leases behind the hub's NAT — so
    the honest recovery probe (``verify.verify`` → ``Verdict.NOT_YET``) passes."""
    # Recovered double-NAT WAN (private) — what the runner reads AFTER rollback.
    hub, fw, armor = FakeHub(), FakeRunner(wan_ip="192.168.30.2"), FakeArmor()
    # Pretend the hub is currently in DMZ (a prior cutover); rollback returns it.
    hub._v[DMZ_PATH] = "on"
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor)
    result = runner.invoke(app, ["net", "single-nat", "--rollback", "--force"])
    assert result.exit_code == 0, result.stdout
    # The provider's rollback ran (DMZ disabled) ...
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "off"
    # ... it rebooted the hub so the disable latched (FIX-5 b) ...
    assert hub.reboot_calls == 1
    # ... and the downstream DHCP re-lease fired so the WAN recovers.
    assert any(tag and "release" in tag[0] for tag in fw.calls)
    # A rollback fires no DMZ-engage / armor install.
    assert (DMZ_PATH, "on") not in hub.set_calls
    assert armor.installed == 0


def test_net_single_nat_rollback_reboots_before_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--rollback` must reboot the hub BEFORE re-leasing DHCP (FIX-5 b): the
    DMZ-disable latches via a reboot, so a re-lease fired before the reboot would
    just re-pull the still-engaged single-NAT address. We assert ordering by having
    the hub's reboot record into the SAME event log the runner writes its tags to."""
    events: list[str] = []

    class EventHub(FakeHub):
        def reboot(self) -> OpResult:
            events.append("hub:reboot")
            return super().reboot()

    class EventRunner(FakeRunner):
        def __call__(self, tag: tuple[str, ...]) -> str:
            if tag and tag[0] == "dhcp_release":
                events.append("runner:dhcp_release")
            return super().__call__(tag)

    hub = EventHub()
    hub._v[DMZ_PATH] = "on"
    fw, armor = EventRunner(wan_ip="192.168.30.2"), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor)
    result = runner.invoke(app, ["net", "single-nat", "--rollback", "--force"])
    assert result.exit_code == 0, result.stdout
    assert "hub:reboot" in events
    assert "runner:dhcp_release" in events
    # The reboot fired strictly before the downstream re-lease.
    assert events.index("hub:reboot") < events.index("runner:dhcp_release")


def test_net_single_nat_rollback_with_persistent_apipa_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--rollback` whose re-lease leaves the WAN STILL APIPA must exit non-zero
    (FIX-5 a): the household is dark and the operator needs the manual-recovery
    signal, not a green ``rolled back`` on a WAN that never came back."""
    hub, fw, armor = FakeHub(), FakeRunner(wan_ip="169.254.5.5"), FakeArmor()
    hub._v[DMZ_PATH] = "on"
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor)
    result = runner.invoke(app, ["net", "single-nat", "--rollback", "--force"])
    assert result.exit_code != 0, result.stdout
    out = result.stdout.lower()
    assert "incomplete" in out or "recover" in out
    # The DMZ-disable still ran first (the dangerous leaf is off) ...
    assert hub.get(DMZ_PATH) == "off"
    # ... but the WAN never recovered, so it is NOT reported as a clean rollback.


def test_net_single_nat_rollback_restores_captured_snapshot_not_fabricated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--rollback` must restore the CAPTURED pre-cutover snapshot (every single-NAT
    leaf disengaged), NOT a fabricated ``{dmz_path: disengaged}`` (FIX-5 c).

    A prior cutover could have engaged BOTH the bridge-mode leaf (old ``single_nat``)
    AND the Advanced-DMZ leaf. A fabricated single-key dict only disables DMZ and
    silently leaves bridge mode ON — the household still behind a single NAT. The
    captured baseline restores BOTH leaves to off, so a rollback drives BOTH writes.
    """
    hub, fw, armor = FakeHub(), FakeRunner(wan_ip="192.168.30.2"), FakeArmor()
    # Both single-NAT leaves currently engaged (a prior cutover left them on).
    hub._v[DMZ_PATH] = "on"
    hub._v[BRIDGE_PATH] = "on"
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor)
    result = runner.invoke(app, ["net", "single-nat", "--rollback", "--force"])
    assert result.exit_code == 0, result.stdout
    # The captured baseline disengaged BOTH leaves — proof it is not the fabricated
    # single-key {dmz: off} dict (which would leave bridge mode stuck on).
    assert (DMZ_PATH, "off") in hub.set_calls
    assert (BRIDGE_PATH, "off") in hub.set_calls
    assert hub.get(DMZ_PATH) == "off"
    assert hub.get(BRIDGE_PATH) == "off"


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


def test_hub_single_nat_passes_real_runner_cleanly_without_firing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deprecation shim passes a REAL/no-op runner cleanly (FIX-6 e).

    The shim builds the REAL Firewalla-bound runner (``_build_runner`` →
    ``system.make_real_runner``) and hands it to ``single_nat_dmz`` for a DRY-RUN
    plan. The whole point of the shim is to STEER, never to mutate — so on this
    apply=False path the runner must NEVER be invoked: the dry-run resolves the DMZ
    op and returns the plan without firing a single runner tag. This matters because
    the real ``make_real_runner`` HARD-FAILS (raises) on any mutating/unknown tag
    when no fw gateway+key is resolved — so if the shim ever fired the runner on the
    dry-run, this would surface as a crash rather than the clean redirect.

    We wire the genuine ``make_real_runner`` (no gateway/key — the fail-closed
    apply-path runner) and wrap it to record every tag it sees. The contract: the
    shim exits 0 with the deprecation note, the real runner is handed in but NEVER
    called, and ZERO device writes happen (the dry-run guardrail).
    """
    from sanctum_cli.net import system

    fired: list[tuple[str, ...]] = []
    # The genuine fail-closed apply-path runner: raises on a mutating/unknown tag.
    real_runner = system.make_real_runner(fw_gateway=None, fw_key=None)

    def recording_real_runner(tag: tuple[str, ...]) -> str:
        fired.append(tag)
        return real_runner(tag)  # would RAISE on a mutating/unknown tag

    hub, armor = FakeHub(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=FakeRunner(), armor=armor)
    # Override _build_runner with the REAL (recording) runner — NOT a FakeRunner —
    # so the shim is proven to hand the genuine fail-closed runner through cleanly.
    monkeypatch.setattr(
        "sanctum_cli.commands.net._build_runner", lambda: recording_real_runner
    )

    result = runner.invoke(app, ["net", "hub", "single-nat"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "deprecat" in out
    assert "single-nat" in out
    # The real runner was handed in but the dry-run shim NEVER fired it …
    assert fired == [], f"deprecation shim fired the runner on a dry-run: {fired}"
    # … and made ZERO device writes (steer, never mutate).
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert hub.rollback_calls == 0
    assert armor.installed == 0


# ── FIX-e: a non-Bell ISP skips the armor + commits a normal public lease ──────


class NormalPublicRunner(FakeRunner):
    """Serves a NORMAL public /24 lease (a non-Bell passthrough — no /1 poison)."""

    def __call__(self, tag: tuple[str, ...]) -> str:
        if tag == ("wan_addr_cidr",):
            self.calls.append(tag)
            return f"2: eth0    inet {self.wan_ip}/24 brd {self.wan_ip} scope global eth0"
        return super().__call__(tag)


def test_single_nat_requires_armor_resolves_per_isp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """The CLI resolves the /32-armor requirement from the configured ISP playbook:
    bell (the default) requires it; a pinned non-Bell ISP does not."""
    from sanctum_cli.commands import net as net_cmd

    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))  # type: ignore[operator]
    assert net_cmd._single_nat_requires_armor() is True  # bell default
    cfg = tmp_path / "instance.yaml"  # type: ignore[operator]
    cfg.write_text("net:\n  isp: generic\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(cfg))
    assert net_cmd._single_nat_requires_armor() is False  # non-Bell → no armor


def test_net_single_nat_non_bell_apply_skips_armor_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """`--apply` on a non-Bell ISP (net.isp: generic) commits a /24 public lease and
    NEVER deploys the /32 armor — the decouple end-to-end through the CLI."""
    cfg = tmp_path / "instance.yaml"  # type: ignore[operator]
    cfg.write_text("net:\n  isp: generic\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(cfg))
    hub, fw, armor = FakeHub(), NormalPublicRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor, out_of_band=True)
    result = runner.invoke(app, ["net", "single-nat", "--apply", "--force"])
    assert result.exit_code == 0, result.stdout
    # DMZ still engaged + committed …
    assert (DMZ_PATH, "on") in hub.set_calls
    assert hub.rollback_calls == 0
    # … but the armor was NEITHER staged NOR installed (skipped for the non-Bell ISP).
    assert armor.installed == 0
    assert armor.staged == 0


# ── FIX-f: the pre-apply box gate (passwordless sudo + dhclient) ───────────────


def test_box_preflight_ready_maps_probe_to_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """``_box_preflight_ready`` resolves host+key the runner uses, consults the SSH
    probe, and maps its (sudo, dhclient) result through the pure gate."""
    from sanctum_cli.commands import net as net_cmd

    key = tmp_path / "fw_key"  # type: ignore[operator]
    key.write_text("k", encoding="utf-8")
    monkeypatch.setattr(net_cmd, "_firewalla_host", lambda: "10.0.0.1")
    monkeypatch.setattr(net_cmd, "_firewalla_key_path", lambda: key)
    monkeypatch.setattr(
        net_cmd.system, "firewalla_box_preflight", lambda *_a, **_k: (True, True)
    )
    assert net_cmd._box_preflight_ready().ok is True
    monkeypatch.setattr(
        net_cmd.system, "firewalla_box_preflight", lambda *_a, **_k: (True, False)
    )
    assert net_cmd._box_preflight_ready().ok is False  # no dhclient → not ready


def test_box_preflight_ready_fail_closed_when_no_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """No SSH key on disk → the gate is not-ready WITHOUT even probing (fail-closed):
    we cannot prove the box is capable, so the cutover must refuse."""
    from sanctum_cli.commands import net as net_cmd

    monkeypatch.setattr(net_cmd, "_firewalla_host", lambda: "10.0.0.1")
    monkeypatch.setattr(net_cmd, "_firewalla_key_path", lambda: tmp_path / "absent")  # type: ignore[operator]
    # Even a probe that WOULD say ready must not be reached without a key.
    monkeypatch.setattr(
        net_cmd.system, "firewalla_box_preflight", lambda *_a, **_k: (True, True)
    )
    assert net_cmd._box_preflight_ready().ok is False


def test_net_single_nat_apply_refuses_when_box_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply` REFUSES (non-zero, zero writes) when the box lacks passwordless sudo
    or a dhclient — the cutover never touches the hub on a box that can't run the ops."""
    from sanctum_cli.devices import flip

    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor, out_of_band=True)
    # Override the _wire ready-stub: the box preflight FAILS.
    monkeypatch.setattr(
        "sanctum_cli.commands.net._box_preflight_ready",
        lambda: flip.PreflightDecision(
            ok=False,
            reason="box preflight FAILED — no DHCP client found on the box "
            "(`dhclient` not on PATH)",
        ),
    )
    result = runner.invoke(app, ["net", "single-nat", "--apply", "--force"])
    assert result.exit_code != 0, result.stdout
    # The clear refusal message is reported to stderr (the standard error path).
    out = (result.stdout + result.stderr).lower()
    assert "preflight" in out or "dhclient" in out
    # Refused BEFORE any mutation: zero writes anywhere (hub never even connected).
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert armor.installed == 0


# ── live-fire config gate: --apply inert until net.single_nat_live is armed ───


def test_net_single_nat_apply_gated_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--apply REFUSES (zero probes/writes) unless net.single_nat_live is armed.

    The staged cutover is proven only against fakes; the live path stays inert until a
    Phase-2 dry-run validates the real hardware. Gated off => the DMZ engage never runs."""
    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor)
    # Override the _wire arming stub: the live gate is OFF (the real default).
    monkeypatch.setattr(
        "sanctum_cli.commands.net._single_nat_live_armed", lambda: False
    )
    result = runner.invoke(app, ["net", "single-nat", "--apply", "--force"])
    assert result.exit_code != 0
    out = (result.stdout + result.stderr).lower()
    assert "gated" in out or "single_nat_live" in out or "phase-2" in out
    # Refused before the hub/box were ever touched.
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert armor.installed == 0
    assert fw.calls == []


def test_single_nat_live_armed_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """_single_nat_live_armed() defaults OFF and flips only on net.single_nat_live: true."""
    from sanctum_cli.commands import net as netmod

    seen = {}

    def fake_instance_value(key, default=None):
        seen["key"], seen["default"] = key, default
        return default  # instance.yaml with the key unset

    monkeypatch.setattr(
        "sanctum_cli.commands.net.config.instance_value", fake_instance_value
    )
    assert netmod._single_nat_live_armed() is False
    assert seen["key"] == "net.single_nat_live"
    assert seen["default"] is False
    monkeypatch.setattr(
        "sanctum_cli.commands.net.config.instance_value",
        lambda key, default=None: True,
    )
    assert netmod._single_nat_live_armed() is True


# ── --check: read-only Phase-2 seam validation ───────────────────────────────


def test_net_single_nat_check_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """--check probes the seams and makes ZERO writes (Phase-2 validation, no --apply)."""
    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor, out_of_band=True)
    result = runner.invoke(app, ["net", "single-nat", "--check"])
    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout.lower()
    assert "read-only" in out
    assert "preflight" in out
    assert "dmz" in out
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert armor.installed == 0


def test_net_single_nat_check_mutually_exclusive_with_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--check and --apply cannot be combined (and combining them writes nothing)."""
    hub, fw, armor = FakeHub(), FakeRunner(), FakeArmor()
    _wire(monkeypatch, hub=hub, fw=fw, armor=armor)
    result = runner.invoke(app, ["net", "single-nat", "--check", "--apply"])
    assert result.exit_code != 0
    assert hub.set_calls == []
