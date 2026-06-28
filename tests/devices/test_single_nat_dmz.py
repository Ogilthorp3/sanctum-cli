"""Layer-2 ``single_nat_dmz`` orchestrator — guarded, dry-run-by-default flip.

``single_nat_dmz`` is the attended Bell **Advanced DMZ + ``/32`` single-NAT**
cutover: it walks the pure :data:`sanctum_cli.devices.flip.FLIP_STAGES` machine,
firing each stage's real I/O through mocked seams — the hub provider
(``set`` DMZ engaged + ``reboot``), the Firewalla runner (WAN→DHCP/PPPoE +
observe the downstream lease), and the armor installer — and composes the whole
sequence behind :func:`sanctum_cli.devices.rails.guarded_apply` so a failed
stage unwinds the flip (disable DMZ → re-lease DHCP) and reports ``ok=False``.

These tests author their expectations from the *consumer's* contract (the stage
order the runbook performs, the armor's ``classify_wan_ip`` WAN vocabulary, the
``guarded_apply`` rails) — never from the production module's own assumptions
(Contracts at the Boundary). Every boundary that the bug could live in is the
*real* thing: the pure flip machine, the real ``guarded_apply`` rails, and the
real ``IntentResult`` shape. Only the genuinely expensive / dangerous edges (a
live hub, a live SSH lease, a live armor install) are mocked — and the mocks
record every write so a dry-run can be proven to make ZERO of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sanctum_cli.devices import flip
from sanctum_cli.devices.base import (
    Capability,
    CapabilityOp,
    DeviceError,
    OpResult,
    Snapshot,
)
from sanctum_cli.devices.sagemcom import _MUTATED_XPATHS, _SAFE_BASELINES

if TYPE_CHECKING:
    from pathlib import Path

# The Bell Advanced-DMZ leaf the cutover engages (the brand's DMZ capability op).
# Verified live: the settable boolean leaf is .../AdvancedDMZ/Enable ('true'/'false').
DMZ_PATH = "Device/Services/BellNetworkCfg/AdvancedDMZ/Enable"


class FakeHub:
    """In-memory Sagemcom-shaped hub: records set/reboot/rollback for assertions.

    Mirrors the real provider's contract surface the orchestrator touches:
    ``capability_op(DMZ)`` returns the Bell (path, engaged) binding, ``set``
    flips the leaf and counts the write, ``reboot`` counts a reboot, and
    ``snapshot``/``rollback`` capture + restore the DMZ leaf. ``set``/``reboot``
    return an :class:`OpResult` (ok=True) so the rails fall through to verify on
    the happy path; a test may flip ``reboot_fails`` to exercise the failure fork.

    Snapshot/rollback mirror the REAL :class:`SagemcomHubProvider`, not a
    convenient shortcut that shares the producer's assumption (Contracts at the
    Boundary). Two fidelity choices matter:

    * The firmware does NOT surface the Advanced-DMZ leaf at snapshot time until a
      ``set`` writes it — the real Bell firmware shape (a getValue of an un-set
      leaf returns None, and the real provider drops a None read). So a fresh hub's
      ``_v`` does NOT seed ``DMZ_PATH``; the earlier fake seeded ``{DMZ_PATH:
      "off"}``, which masked the bug by making the leaf always present.
    * ``snapshot`` therefore reproduces the real provider's hard guarantee:
      every leaf in the PRODUCTION ``_MUTATED_XPATHS`` gets a ``_SAFE_BASELINE``
      even when its read is absent. Deriving the guarantee from the production
      tuple (a different source than the producer's snapshot body) is what makes
      this test fail until DMZ is actually added to ``_MUTATED_XPATHS``.
    * ``rollback`` re-issues a ``set`` per captured leaf (driving the recorded
      write path) and reports ``ok=False`` on an empty baseline — exactly the
      real provider — so "rollback drove DMZ off" is proven from a real restore,
      not a dict swap that would silently lose an omitted key.
    """

    kind = "hub"
    brand = "fake-bell-hub"

    def __init__(self) -> None:
        # The firmware does NOT surface the DMZ leaf until it is written — the real
        # shape. A fresh hub reads None for DMZ_PATH (absent from _v).
        self._v: dict[str, str] = {}
        self.set_calls: list[tuple[str, str]] = []
        self.reboot_calls = 0
        self.rollback_calls = 0
        self.reboot_fails = False

    @staticmethod
    def detect(net: object) -> float:
        return 1.0

    def connect(self, creds: object | None) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def get(self, path: str) -> str | None:
        return self._v.get(path)

    def set(self, path: str, value: str) -> OpResult:
        before = self._v.get(path)
        self._v[path] = value
        self.set_calls.append((path, value))
        return OpResult(ok=True, detail=f"set {path}", before=before, after=value)

    def reboot(self) -> OpResult:
        self.reboot_calls += 1
        if self.reboot_fails:
            msg = "hub rejected reboot"
            raise DeviceError(msg)
        return OpResult(ok=True, detail="reboot issued")

    def capabilities(self) -> set[Capability]:
        return {Capability.READ, Capability.SET, Capability.DMZ, Capability.REBOOT}

    def capability_op(self, capability: Capability) -> CapabilityOp | None:
        if capability is Capability.DMZ:
            return CapabilityOp(path=DMZ_PATH, engaged="true")  # live: 'true'/'false'
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        # Best-effort capture of what the firmware surfaces (None reads dropped) …
        data = {path: value for path, value in self._v.items() if value is not None}
        # … then the REAL provider's hard guarantee: every leaf the cutover MUTATES
        # gets a restorable baseline even when unread. Derived from the PRODUCTION
        # tuple+baselines so this fake fails until DMZ is actually in _MUTATED_XPATHS.
        for path in _MUTATED_XPATHS:
            data.setdefault(path, _SAFE_BASELINES[path])
        return Snapshot(brand=self.brand, taken_at="t", data=data)

    def rollback(self, snap: Snapshot) -> OpResult:
        self.rollback_calls += 1
        if not snap.data:
            return OpResult(ok=False, detail="rollback failed: no restorable baseline")
        for path, value in snap.data.items():
            self.set(path, value)
        return OpResult(ok=True, detail=f"rolled back {len(snap.data)} key(s)")


class FakeRunner:
    """Records every runner op the orchestrator fires; serves a scripted lease.

    The orchestrator drives the Firewalla over the same ``Runner`` abstraction
    the net layer uses (``Callable[[tuple[str, ...]], str]``). This fake records
    every tag fired (so a dry-run can be asserted to fire ZERO mutating ops) and
    returns a scripted downstream WAN address for the observe-lease probe, in the
    armor's ``classify_wan_ip`` vocabulary's terms (a public IP → "public").
    """

    def __init__(self, wan_ip: str = "203.0.113.7") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.wan_ip = wan_ip

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            return self.wan_ip
        # FIX (c): a fake that serves a 'public' lease must model the WHOLE contract
        # the real box serves — the /32 armor + a clean route table — or the poison
        # gate (correctly) refuses to commit. Healthy armored state by default.
        if tag == ("wan_addr_cidr",):
            return f"2: eth0    inet {self.wan_ip}/32 brd {self.wan_ip} scope global eth0"
        if tag == ("wan_routes",):
            return "default via 10.0.0.1 dev eth0"  # no 0.0.0.0/1 poison route
        return ""


class ScriptedLeaseRunner:
    """Records every tag and serves a SCRIPTED SEQUENCE of downstream leases.

    Unlike :class:`FakeRunner` (one fixed lease), this serves a queue of WAN-IP
    strings — one per ``lease_observe`` read — so a test can model the real
    transient the retry exists for: the router grabs a ``169.254.x`` APIPA on the
    first DHCP after the hub reboot, then a single re-lease (``dhcp_release``)
    clears it and the second read returns a public lease.

    The queue is consumed in order on each ``lease_observe``; once exhausted the
    last value sticks (a persistent-bad lease that never clears). Every tag is
    recorded so a test can assert EXACTLY how many re-leases fired between the
    reads — the "exactly ONE re-lease" contract.
    """

    def __init__(self, leases: list[str]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._leases = list(leases)
        self._last_lease = "203.0.113.7"

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            lease = self._leases.pop(0) if len(self._leases) > 1 else (
                self._leases[0] if self._leases else ""
            )
            if lease:
                self._last_lease = lease
            return lease
        # FIX (c): model the WHOLE contract a 'public' lease carries — the /32 armor
        # holding + a clean route table — so a committing cutover is proven armored,
        # not just public. (A poisoned readback is modelled by a dedicated runner.)
        if tag == ("wan_addr_cidr",):
            return f"3: eth0    inet {self._last_lease}/32 brd {self._last_lease} scope global eth0"
        if tag == ("wan_routes",):
            return "default via 10.0.0.1 dev eth0"  # no 0.0.0.0/1 poison route
        return ""

    def lease_reads(self) -> int:
        return sum(1 for t in self.calls if t and t[0] == "lease_observe")

    def releases_between_first_two_reads(self) -> int:
        """How many ``dhcp_release`` ops fired strictly between read #1 and read #2."""
        read_idxs = [i for i, t in enumerate(self.calls) if t and t[0] == "lease_observe"]
        if len(read_idxs) < 2:
            return 0
        window = self.calls[read_idxs[0] + 1 : read_idxs[1]]
        return sum(1 for t in window if t and t[0] == "dhcp_release")


class FakeArmor:
    """Mock single-NAT armor installer: records stage + install, never touches disk.

    Two phases (FIX-2): ``stage()`` is the PRE-DMZ deploy + structural armed verify
    (the /32 hook lands on the box while the LAN is healthy); ``install()`` is the
    post-cutover HEALTHY confirm. Both are recorded so a test can prove staging
    happened before DMZ engaged, and that a dry-run fires neither.
    """

    def __init__(self, *, ok: bool = True, stage_ok: bool = True) -> None:
        self.installed = 0
        self.staged = 0
        self._ok = ok
        self._stage_ok = stage_ok

    def stage(self) -> OpResult:
        self.staged += 1
        if not self._stage_ok:
            return OpResult(ok=False, detail="armor stage failed")
        return OpResult(ok=True, detail="armor staged")

    def install(self) -> OpResult:
        self.installed += 1
        if not self._ok:
            return OpResult(ok=False, detail="armor install failed")
        return OpResult(ok=True, detail="armor installed")


def _all_pass_verifiers() -> dict[str, object]:
    """A stage→verifier map where every stage's probe passes (the happy path)."""
    return {stage: (lambda: True) for stage in flip.FLIP_STAGES}


@pytest.fixture(autouse=True)
def _fast_settle_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the observe_lease settle poll (FIX a) with a TINY real timeout + a short
    real interval so the e2e cutover tests exercise the REAL bounded poll loop + the
    REAL ``time.monotonic`` clock instantly — no time-mocking. A transient lease that
    never clears hard-fails in ~50 ms (a handful of real ``time.sleep(0.005)`` ticks);
    a transient-then-public settles on the next read. The direct ``_observe_lease``
    driver tests above inject their own clock and are unaffected (they pass explicit
    ``timeout_s``/``now``/``sleep``). The CLI path uses the real 360 s default."""
    from sanctum_cli.devices import intents

    monkeypatch.setattr(intents, "_SETTLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(intents, "_SETTLE_POLL_INTERVAL_S", 0.005)
    # FIX (a-2): shrink the ACTIVE box-op dark-window ride too (wan_dhcp re-lease +
    # the rollback's dhcp_release) so the e2e tests that exercise a flaky/never-
    # returning box run instantly against the REAL bounded ride loop + the REAL
    # ``time.monotonic`` clock — no time-mocking. A box that never returns fails
    # closed in ~0.2 s; a transient-then-reachable box rides through it. The direct
    # ``_ride_dark_window`` driver tests below inject their own clock and are
    # unaffected. The CLI path uses the real 480 s default.
    monkeypatch.setattr(intents, "_BOX_OP_TIMEOUT_S", 0.2)
    monkeypatch.setattr(intents, "_BOX_OP_POLL_INTERVAL_S", 0.005)


# ── dry-run: the hard guardrail (ZERO device writes) ─────────────────────────


def test_dmz_dry_run_makes_zero_device_writes() -> None:
    """apply=False returns the staged plan and fires NO set/reboot/runner/armor."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    res = single_nat_dmz(hub, runner, armor, apply=False)
    assert res.applied is False
    assert res.result is None
    # Zero mutations anywhere — the overnight-build guardrail.
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert hub.rollback_calls == 0
    assert armor.installed == 0
    assert armor.staged == 0  # the armor is NOT even staged on a dry-run
    assert runner.calls == []
    assert hub.get(DMZ_PATH) is None  # untouched (firmware never surfaced the leaf)


def test_dmz_dry_run_plan_names_the_stages() -> None:
    """The staged plan describes the DMZ cutover stages for the operator."""
    from sanctum_cli.devices.intents import single_nat_dmz

    res = single_nat_dmz(FakeHub(), FakeRunner(), FakeArmor(), apply=False)
    plan_text = "\n".join(res.plan).lower()
    assert "dmz" in plan_text
    # The plan reflects the real flip stages (observe lease, armor, reboot).
    assert "reboot" in plan_text
    assert "armor" in plan_text


# ── out-of-band gate: refuse with zero writes when no recovery path ──────────


def test_dmz_refuses_without_out_of_band_path_zero_writes() -> None:
    """out_of_band_reachable=False → refuse the flip, mutate NOTHING.

    The cutover drops the WAN; without an out-of-band recovery path a misstep
    could strand the household dark with no way back in (flip.gate_ok)."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=False,
        force=True,
    )
    assert res.applied is False
    assert res.result is not None
    assert res.result.ok is False
    assert "out-of-band" in res.result.detail.lower() or "out of band" in res.result.detail.lower()
    # Refused BEFORE any mutation: zero writes anywhere.
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert hub.rollback_calls == 0
    assert armor.installed == 0
    assert runner.calls == []


# ── apply happy path: walks the stages through guarded_apply ─────────────────


def test_dmz_apply_walks_stages_and_commits(tmp_path: Path) -> None:
    """apply=True + every stage verify passing → DMZ engaged, hub rebooted, armor
    installed, lease observed, and the whole thing committed via guarded_apply."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is True
    # The flip engaged Advanced DMZ via the provider's OWN capability op.
    assert (DMZ_PATH, "true") in hub.set_calls
    assert hub.get(DMZ_PATH) == "true"
    # It rebooted the hub for the new WAN/DMZ config to take.
    assert hub.reboot_calls == 1
    # It installed the armor kit.
    assert armor.installed == 1
    # A successful flip never rolls back.
    assert hub.rollback_calls == 0
    # The armor was STAGED (pre-DMZ) on the apply path.
    assert armor.staged == 1
    # It drove the runner (WAN→DHCP + observe-lease).
    assert runner.calls


def test_dmz_stages_armor_before_engaging_dmz(tmp_path: Path) -> None:
    """FIX-2 (the ORDERING contract at the orchestrator): the flip records the armor
    stage BEFORE it engages Advanced DMZ.

    A recording hub + recording armor write into ONE shared event log, so the
    relative order of ``armor_stage`` and the DMZ-engage ``set`` is observable. The
    armor MUST be staged first (the /32 hook on the box while the LAN is healthy) so
    the un-armored poison-/1 window the 06-26 strand fell into is structurally
    impossible. Tests the CONTRACT (a real ``set`` argv vs a real ``stage`` call),
    not the FLIP_STAGES field alone — driving the real ``_run_stage`` walk.
    """
    from sanctum_cli.devices.intents import single_nat_dmz

    events: list[str] = []

    class OrderHub(FakeHub):
        def set(self, path: str, value: str) -> OpResult:
            if path == DMZ_PATH and value == "true":
                events.append("dmz_engage")
            return super().set(path, value)

    class OrderArmor(FakeArmor):
        def stage(self) -> OpResult:
            events.append("armor_stage")
            return super().stage()

    hub, runner, armor = OrderHub(), FakeRunner(), OrderArmor()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is True
    # The armor stage and the DMZ-engage both happened …
    assert "armor_stage" in events
    assert "dmz_engage" in events
    # … and the armor was staged STRICTLY before the DMZ engaged (no un-armored /1).
    assert events.index("armor_stage") < events.index("dmz_engage")


def test_dmz_stage_armor_failure_rolls_back_before_dmz_engaged(tmp_path: Path) -> None:
    """A failed armor STAGE (pre-DMZ) unwinds the flip WITHOUT ever engaging DMZ.

    Because staging now precedes enable_dmz, a stage that reports ok=False must trip
    the rails before the DMZ is touched — so the worst case is a clean, still-double-
    NAT box, never a half-engaged un-armored cutover.
    """
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor(stage_ok=False)
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is False
    assert armor.staged == 1  # it tried to stage
    assert armor.installed == 0  # never reached the post-cutover install
    # DMZ was NEVER engaged (the stage failed before enable_dmz).
    assert (DMZ_PATH, "true") not in hub.set_calls


# ── FIX-3: the prevent-interlock at the enable_dmz seam (fail-closed) ─────────
#
# The authoritative gate fires AT THE MOMENT DMZ is engaged, not just once at the
# top of flight: a channel can die between preflight and enable_dmz. ``oob_probe``
# is the live moment-of-op Tailscale re-check (injectable so it is testable
# offline). When it (or any precondition) is down, the interlock refuses and the
# DMZ ``set`` is NEVER fired — the flip fails closed, never fail-to-DARK.


def test_dmz_interlock_refuses_engage_when_moment_of_op_oob_probe_down(
    tmp_path: Path,
) -> None:
    """FIX-3: a live OOB probe that is DOWN at the moment of engage refuses the DMZ
    set — even though the top-of-flight ``out_of_band_reachable`` gate passed.

    This is the 06-26 lesson encoded: the LAN was up at gate-check time, then
    collapsed. The moment-of-op interlock re-probes the LAN-INDEPENDENT channel and
    refuses to engage DMZ if it is not live RIGHT NOW — so ``set(DMZ, engaged)`` is
    never called and the household is never stranded behind an un-recoverable WAN.
    """
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,  # the cheap top-of-flight gate passes …
        oob_probe=lambda: False,  # … but the live moment-of-op re-probe is DOWN
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is False
    # The DMZ was NEVER engaged — the interlock refused before the set() fired.
    assert (DMZ_PATH, "true") not in hub.set_calls
    # The armor WAS staged (pre-DMZ) before the interlock refused at enable_dmz …
    assert armor.staged == 1
    # … but the post-cutover install never ran (we never got past enable_dmz).
    assert armor.installed == 0
    # The flip unwound on the still-disengaged baseline (idempotent — WAN intact).
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "false"


def test_dmz_interlock_is_not_waived_by_force(tmp_path: Path) -> None:
    """``--force`` waives only the human confirm; it must NEVER waive the interlock.

    With force=True and the live OOB probe down, the flip must STILL refuse to
    engage DMZ — force is not a bypass for the recovery-path safety gate.
    """
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        oob_probe=lambda: False,
        force=True,  # force does NOT waive the interlock
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is False
    assert (DMZ_PATH, "true") not in hub.set_calls


def test_dmz_interlock_engages_when_oob_probe_live(tmp_path: Path) -> None:
    """The happy moment-of-op path: a live OOB probe lets the interlock engage DMZ.

    Mirror of the refusal test — with the live probe TRUE (and armor staged +
    rollback staged, both true by construction at enable_dmz), the interlock engages
    and the cutover commits. Proves the gate is not stuck-closed.
    """
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        oob_probe=lambda: True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is True
    assert (DMZ_PATH, "true") in hub.set_calls
    assert hub.rollback_calls == 0


def test_dmz_apply_confirm_declined_makes_zero_writes(tmp_path: Path) -> None:
    """apply=True without force and a declining confirm aborts with no mutation."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=False,
        confirm=lambda _plan: False,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is False
    assert hub.set_calls == []
    assert hub.reboot_calls == 0
    assert armor.installed == 0


# ── apply failure forks: a failed stage unwinds the whole flip ───────────────


def test_dmz_stage_verify_failure_rolls_back(tmp_path: Path) -> None:
    """A stage whose verify probe FAILS trips guarded_apply rollback: DMZ disabled
    (provider.rollback) AND a DHCP re-lease fired on the runner — and ok=False."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    verifiers = _all_pass_verifiers()
    # The observe-lease stage's verify fails (downstream never got a public lease).
    verifiers["observe_lease"] = lambda: False
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=verifiers,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is False
    # The flip unwound: DMZ rolled back to "off".
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "false"
    # And the rollback re-leased DHCP downstream (disable DMZ → re-lease).
    assert any(tag and "release" in tag[0] for tag in runner.calls)


def test_dmz_rollback_drives_dmz_off_even_when_firmware_omits_it(tmp_path: Path) -> None:
    """The CRITICAL fix: even when the hub's snapshot OMITS the DMZ leaf (the real
    firmware shape — a getValue of the un-engaged leaf returns None), a failed
    cutover's rollback MUST still drive DMZ → off.

    This is the fail-to-DARK trap: the cutover engages Advanced DMZ (single-NAT),
    then a stage fails. If the pre-cutover snapshot never carried a DMZ baseline,
    the rollback has nothing to restore for DMZ and silently "succeeds" while the
    hub stays in single-NAT — the household left dark with no recovery path. The
    snapshot MUST guarantee a DMZ baseline of "off" (DMZ in ``_MUTATED_XPATHS``)
    so rollback re-issues ``set(DMZ_PATH, "false")``.

    ``FakeHub`` here models the real firmware: its ``_v`` does NOT surface the DMZ
    leaf until written, so the snapshot the rails take is built purely from the
    PRODUCTION ``_MUTATED_XPATHS`` baseline guarantee — exactly the seam the fix
    repairs.
    """
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    # Pre-cutover: the firmware does not surface the DMZ leaf at all.
    assert hub.get(DMZ_PATH) is None

    verifiers = _all_pass_verifiers()
    verifiers["observe_lease"] = lambda: False  # a stage fails → unwind
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=verifiers,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is False
    # The cutover engaged DMZ …
    assert (DMZ_PATH, "true") in hub.set_calls
    # … and despite the firmware never surfacing the leaf at snapshot time, the
    # rollback drove it back OFF (proof the snapshot carried the guaranteed
    # baseline and rollback re-issued the restore write).
    assert hub.rollback_calls == 1
    assert (DMZ_PATH, "false") in hub.set_calls
    assert hub.get(DMZ_PATH) == "false"


def test_dmz_reboot_failure_rolls_back(tmp_path: Path) -> None:
    """A stage whose I/O RAISES (the hub rejects the reboot) trips rollback too —
    a raised change is the worst case the rails exist to catch.

    With ``reboot_fails`` permanent, the rollback's OWN latch-reboot (FIX-5 b)
    also fails, so the recovery is honestly reported INCOMPLETE — but the inner
    DMZ-disable still ran first, so the dangerous leaf is off."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()
    hub.reboot_fails = True
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is False
    # Two reboots attempted: the cutover's hub_reboot stage, then the rollback's
    # own latch-reboot — both rejected by this hub, so recovery is incomplete.
    assert hub.reboot_calls == 2
    assert hub.rollback_calls == 1  # and unwound
    # The inner DMZ-disable ran before the latch-reboot was attempted, so the
    # dangerous leaf is off even though the latch-reboot then failed.
    assert hub.get(DMZ_PATH) == "false"
    # Armor must NOT have been installed if the reboot (earlier stage) failed.
    assert armor.installed == 0


def test_dmz_armor_install_failure_rolls_back(tmp_path: Path) -> None:
    """The armor installer reporting ok=False is a failed stage → rollback + ok=False."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor(ok=False)
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is False
    assert armor.installed == 1  # it tried
    assert hub.rollback_calls == 1  # and unwound
    assert hub.get(DMZ_PATH) == "false"


# ── observe_lease: classify the REAL lease, retry APIPA exactly once ─────────
#
# These prove the FIX-4 wiring: the observe_lease stage captures the runner's
# lease, classifies it with flip.classify_wan_ip, and on a retryable
# (apipa/none) lease fires EXACTLY ONE re-lease + re-observe (flip.should_retry_apipa);
# a persistent retryable lease raises and the rails roll back. Every verifier
# PASSES here, so the retry/rollback behavior can ONLY come from the stage's own
# lease classification — not from a verifier returning False (which is a separate
# seam the older tests already cover).


def test_observe_lease_apipa_then_public_fires_exactly_one_release_and_proceeds(
    tmp_path: Path,
) -> None:
    """The real transient: read #1 is a 169.254.x APIPA, ONE re-lease clears it,
    read #2 is public → the flip proceeds to commit. Exactly one re-lease fires
    between the two reads, the cutover does NOT roll back, and DMZ stays engaged."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    # APIPA first, public after the single re-lease.
    runner = ScriptedLeaseRunner(["169.254.10.5", "203.0.113.7"])
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),  # every verifier passes
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is True
    # EXACTLY one re-lease fired between the first and second lease reads.
    assert runner.releases_between_first_two_reads() == 1
    # The stage re-observed (two reads total: APIPA, then public).
    assert runner.lease_reads() == 2
    # The transient cleared → committed, NOT rolled back; DMZ stayed engaged.
    assert hub.rollback_calls == 0
    assert hub.get(DMZ_PATH) == "true"
    assert armor.installed == 1


def test_observe_lease_persistent_apipa_rolls_back_past_the_settle_window(tmp_path: Path) -> None:
    """A 169.254.x APIPA that NEVER clears through the whole settle window (FIX a) is
    a GENUINE failure: the bounded poll waits through the window, nudging a re-lease
    each tick, then hard-fails at the bound so the rails unwind — DMZ rolled back to
    off, ok=False — without looping forever. (The fast-settle fixture shrinks the
    window to a handful of real ``time.sleep`` ticks; the precise tick/re-lease counts
    are pinned by the deterministic direct ``_observe_lease`` driver test above.)"""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    # APIPA on every read — the re-lease never clears it, the whole window through.
    runner = ScriptedLeaseRunner(["169.254.10.5"])
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),  # verifiers pass; the STAGE fails
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.applied is True
    assert res.result is not None
    assert res.result.ok is False
    # It polled through the window (more than one read) and nudged a re-lease per tick
    # — bounded, not an infinite loop — then gave up.
    assert runner.lease_reads() >= 2
    assert runner.releases_between_first_two_reads() == 1
    # Persistent APIPA → the flip unwound: DMZ disabled, household not left dark.
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "false"
    # Armor is a LATER stage than observe_lease — it must never have run.
    assert armor.installed == 0


def test_observe_lease_empty_lease_retries_then_rolls_back(tmp_path: Path) -> None:
    """An empty/no lease (classify ``none``) is retryable exactly like APIPA: one
    re-lease, and if it is still empty the flip rolls back (fail-closed, not a
    silent commit on a dead WAN)."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    runner = ScriptedLeaseRunner([""])  # never pulls a lease
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is False
    assert runner.releases_between_first_two_reads() == 1
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "false"


def test_observe_lease_public_first_read_fires_no_release(tmp_path: Path) -> None:
    """A public lease on the FIRST read is the single-NAT win: the stage proceeds
    with NO re-lease at all (a re-lease would needlessly bounce a healthy WAN)."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    runner = ScriptedLeaseRunner(["203.0.113.7"])
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is True
    # Only ONE lease read (no re-observe) and ZERO re-leases.
    assert runner.lease_reads() == 1
    assert not any(t and t[0] == "dhcp_release" for t in runner.calls)
    assert hub.rollback_calls == 0
    assert hub.get(DMZ_PATH) == "true"


def test_observe_lease_double_nat_first_read_hard_fails_in_the_stage_itself(
    tmp_path: Path,
) -> None:
    """A double_nat (RFC1918) lease is a DEFINITE failure (FIX a): re-leasing a
    hub-handed private address cannot help and waiting on it cannot help, so the
    settle poll hard-fails AT ONCE inside the stage — fired no re-lease of its own,
    never waited on it. Every verifier PASSES here, so the rollback can ONLY come
    from the stage's own lease classification (not from a verifier returning False)."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    runner = ScriptedLeaseRunner(["192.168.2.10"])  # hub's own LAN → double_nat
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),  # every verifier passes; the STAGE fails
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is False
    # double_nat hard-failed on the first read → the stage fired no re-lease of its
    # own before raising (read it exactly once, no dhcp_release in the stage window).
    assert runner.lease_reads() == 1
    assert not any(t and t[0] == "dhcp_release" for t in runner.calls[: runner.lease_reads() + 1])
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "false"


# ── FIX (a): the bounded settle/poll driver (real runner + injected fake clock) ─
#
# These drive intents._observe_lease DIRECTLY with an injected monotonic clock + a
# no-op sleep against the REAL ScriptedLeaseRunner boundary (no time-mocking, no
# subprocess) so every branch is deterministic and instant. The hostile input is
# the 06-26 hub-dark window: a cutover that takes ~90s to settle must NOT false-fail
# at t+5s, and a transient that NEVER clears must hard-fail at the bound (not loop
# forever, not commit).


class _FakeClock:
    """A monotonic clock double: returns each scripted value in turn, then sticks.

    The poll calls ``now()`` once at start then once per tick; a list of increasing
    values drives ``elapsed = now() - start`` deterministically with no real time.
    """

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._last = 0.0

    def __call__(self) -> float:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


def _relelease_count(runner: ScriptedLeaseRunner) -> int:
    return sum(1 for t in runner.calls if t and t[0] == "dhcp_release")


def test_observe_lease_settles_to_public_within_window_no_false_fail() -> None:
    """REGRESSION for (a): leases [apipa, apipa, public] read at 30s/60s/90s (all <
    the 360s bound) SETTLES to public — it must NOT false-fail mid-window. Proves a
    cutover that takes ~90s to settle no longer false-fails at t+5s. Re-leases fired
    between reads; the real ScriptedLeaseRunner boundary + a real monotonic-shaped
    fake clock, no time-mocking of the impl."""
    from sanctum_cli.devices import intents

    runner = ScriptedLeaseRunner(["169.254.10.5", "169.254.10.5", "203.0.113.7"])
    clock = _FakeClock([0.0, 30.0, 60.0, 90.0])
    sleeps: list[float] = []
    # Returns normally (no _StageError) → the cutover would COMMIT.
    intents._observe_lease(
        runner, timeout_s=360.0, poll_interval_s=15.0, now=clock, sleep=sleeps.append
    )
    assert runner.lease_reads() == 3  # apipa, apipa, public
    assert _relelease_count(runner) == 2  # one nudge after each transient read
    assert len(sleeps) == 2  # slept once per keep-polling tick, never after settling


def test_observe_lease_persistent_transient_hard_fails_at_bound_not_forever() -> None:
    """A transient that NEVER clears hard-fails at the bound — bounded (not an
    infinite re-lease loop) and surfaced as a GENUINE failure only AFTER the window
    proves it is not merely settling."""
    from sanctum_cli.devices import intents

    runner = ScriptedLeaseRunner(["169.254.10.5"])  # apipa sticks forever
    clock = _FakeClock([0.0, 100.0, 200.0, 300.0, 400.0])  # crosses 360 on tick 4
    sleeps: list[float] = []
    with pytest.raises(intents._StageError) as ei:
        intents._observe_lease(
            runner, timeout_s=360.0, poll_interval_s=15.0, now=clock, sleep=sleeps.append
        )
    assert "never came up" in str(ei.value).lower() or "still" in str(ei.value).lower()
    # Bounded: it polled a handful of times then gave up — never looped forever.
    assert 1 <= len(sleeps) <= 5
    assert _relelease_count(runner) == len(sleeps)  # one nudge per keep-polling tick


def test_observe_lease_double_nat_first_read_hard_fails_immediately() -> None:
    """A double_nat lease is a DEFINITE failure (DMZ did not take) — the stage raises
    AT ONCE: zero re-leases, zero sleeps, never waited on."""
    from sanctum_cli.devices import intents

    runner = ScriptedLeaseRunner(["192.168.2.10"])  # hub's own LAN → double_nat
    clock = _FakeClock([0.0, 1.0])
    sleeps: list[float] = []
    with pytest.raises(intents._StageError) as ei:
        intents._observe_lease(
            runner, timeout_s=360.0, poll_interval_s=15.0, now=clock, sleep=sleeps.append
        )
    assert "double" in str(ei.value).lower() or "did not take" in str(ei.value).lower()
    assert runner.lease_reads() == 1
    assert _relelease_count(runner) == 0
    assert sleeps == []


def test_observe_lease_public_first_read_no_relelease_no_sleep() -> None:
    """A public lease on the first read settles at once: one read, no re-lease, no
    sleep — a healthy WAN is never needlessly bounced."""
    from sanctum_cli.devices import intents

    runner = ScriptedLeaseRunner(["203.0.113.7"])
    clock = _FakeClock([0.0, 1.0])
    sleeps: list[float] = []
    intents._observe_lease(
        runner, timeout_s=360.0, poll_interval_s=15.0, now=clock, sleep=sleeps.append
    )
    assert runner.lease_reads() == 1
    assert _relelease_count(runner) == 0
    assert sleeps == []


def test_observe_lease_transient_lan_blip_reads_as_settling_not_instant_fail() -> None:
    """A runner that RAISES RuntimeError (a LAN-SSH blip during the reboot) on the
    first read is treated as 'still settling' (classified none), NOT an instant fail
    — then the LAN recovers and a public lease settles the poll. This is the (b)
    overlap handled fail-soft within the bounded window (it would hard-fail at the
    bound if the LAN stayed dark, never false-commit)."""
    from sanctum_cli.devices import intents

    class _BlipThenPublicRunner(ScriptedLeaseRunner):
        def __init__(self) -> None:
            super().__init__(["203.0.113.7"])
            self._blipped = False

        def __call__(self, tag: tuple[str, ...]) -> str:
            if tag and tag[0] == "lease_observe" and not self._blipped:
                self._blipped = True
                self.calls.append(tag)
                msg = "ssh: connect to host failed"
                raise RuntimeError(msg)
            return super().__call__(tag)

    runner = _BlipThenPublicRunner()
    clock = _FakeClock([0.0, 20.0, 40.0])
    sleeps: list[float] = []
    intents._observe_lease(
        runner, timeout_s=360.0, poll_interval_s=15.0, now=clock, sleep=sleeps.append
    )
    # The blip read as 'none' (still settling), polled again, then settled on public.
    assert len(sleeps) == 1


# ── FIX (c): netmask/route poison gate — a poisoned-but-public lease never commits ─
#
# Bell's Advanced DMZ hands a PUBLIC IP carrying a /1 netmask + a 0.0.0.0/1 on-link
# route that collapses LAN forwarding. classify_wan_ip reads only the bare IPv4 (it
# strips the prefix), so a "public" lease that is actually poisoned — the /32 armor
# NOT holding — would commit GREEN on a dead LAN (the exact 2026-06-26 condition).
# These drive the FULL single_nat_dmz orchestrator with a runner that serves a public
# lease but a POISONED route-state, and assert the cutover ROLLS BACK (never green);
# the armored counterpart proves the genuine single-NAT win still commits.


class PoisonedPublicRunner:
    """Serves a PUBLIC lease but a POISONED netmask/route-state (the 06-26 trap).

    ``lease_observe``/``fw_wan_ip`` return a public IP (so ``classify_wan_ip`` →
    ``"public"`` → the old path would COMMIT), but the raw readbacks the poison gate
    inspects carry Bell's poison: a ``/1`` WAN netmask and a ``0.0.0.0/1`` route. The
    /32 armor did NOT hold — committing would strand the household on a dead LAN.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            return "24.150.33.7"  # a real public IP → classify "public"
        if tag == ("wan_addr_cidr",):
            return "2: eth0    inet 24.150.33.7/1 brd 127.255.255.255 scope global eth0"
        if tag == ("wan_routes",):
            return "default via 10.111.0.1 dev eth0\n0.0.0.0/1 via 10.111.0.1 dev eth0"
        return ""


class ArmoredPublicRunner:
    """Serves a PUBLIC lease WITH the /32 armor holding + a clean route table.

    The genuine single-NAT win the cutover is allowed to commit: a public lease, the
    WAN pinned to /32 (the armor's address-supersede held), and no 0.0.0.0/1 route.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            return "24.150.33.7"
        if tag == ("wan_addr_cidr",):
            return "2: eth0    inet 24.150.33.7/32 brd 24.150.33.7 scope global eth0"
        if tag == ("wan_routes",):
            return "default via 10.111.0.1 dev eth0"
        return ""


def test_observe_lease_public_but_poisoned_netmask_rolls_back_never_commits_green(
    tmp_path: Path,
) -> None:
    """THE 06-26 CONDITION: a 'public' lease that is actually carrying Bell's /1
    poison (the /32 armor did NOT hold) must NEVER commit green — the poison gate
    inspects the netmask + route and the cutover ROLLS BACK instead of committing on
    a dead LAN."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    runner = PoisonedPublicRunner()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),  # every verifier passes; the GATE fails
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is False  # NOT green
    # The rails unwound: DMZ rolled back to off.
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "false"
    # The poison gate ran BEFORE the post-cutover armor install — never reached it.
    assert armor.installed == 0
    # It really inspected the netmask AND the route table (not just the bare IP).
    assert ("wan_addr_cidr",) in runner.calls
    assert ("wan_routes",) in runner.calls


def test_observe_lease_public_and_armored_commits_green(tmp_path: Path) -> None:
    """The armored counterpart: a public lease WITH the /32 armor holding + a clean
    route table is the genuine single-NAT win — it COMMITS (ok=True), no rollback."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    runner = ArmoredPublicRunner()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is True
    assert hub.rollback_calls == 0
    assert hub.get(DMZ_PATH) == "true"
    assert armor.installed == 1
    # The gate inspected the armored netmask + clean routes and let it through.
    assert ("wan_addr_cidr",) in runner.calls
    assert ("wan_routes",) in runner.calls


def test_dmz_unsupported_provider_raises_legibly() -> None:
    """A hub with no DMZ capability op fails legibly, mutating nothing."""
    from sanctum_cli.devices.intents import single_nat_dmz

    class NoDmzHub(FakeHub):
        def capability_op(self, capability: Capability) -> CapabilityOp | None:
            return None

    hub, runner, armor = NoDmzHub(), FakeRunner(), FakeArmor()
    with pytest.raises(DeviceError) as ei:
        single_nat_dmz(hub, runner, armor, apply=False)
    assert "dmz" in str(ei.value).lower()
    assert hub.set_calls == []


# ── _DmzRollbackProvider.rollback: HONEST, reboot-aware, verified recovery ────
#
# The rails own the rollback contract, but the Advanced-DMZ unwind is more than a
# disable + a blind re-lease. Engaging DMZ needs a hub reboot to LATCH, so the
# DISABLE needs one too; and a swallowed re-lease that left the WAN still-APIPA
# (or never came back) must NOT be reported green — it has stranded the household
# dark and the operator needs the manual-recovery signal. These tests pin three
# contracts the council blocked the build on (FIX-5 a + b):
#
#   (a) rollback with a still-APIPA re-lease  -> ok=False (manual recovery)
#   (b) rollback reboots the hub BEFORE the re-lease, then verifies recovery
#   ()  rollback that recovers a working double-NAT lease -> ok=True
#
# The recovery verification is the REAL ``sanctum_cli.net.verify.verify`` over the
# rollback runner (a recovered network is double-NAT -> Verdict.NOT_YET; APIPA is
# Verdict.APIPA_ROLLBACK), so the green/red verdict is derived from the consumer's
# real contract — never a lambda:True (honest-verify).


class EventHub(FakeHub):
    """A FakeHub that appends every reboot/rollback to a SHARED ordered event log.

    The rollback recovery contract is an ORDERING claim — disable DMZ, reboot the
    hub so the disable latches, THEN re-lease downstream — so a test must be able
    to assert reboot happened strictly before the re-lease. The hub and the runner
    write into the same ``events`` list so the relative order of ``hub:reboot`` and
    ``runner:dhcp_release`` is observable (mirrors the real sequencing: the hub
    reboot and the Firewalla re-lease are two different transports).
    """

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def reboot(self) -> OpResult:
        self._events.append("hub:reboot")
        return super().reboot()

    def rollback(self, snap: Snapshot) -> OpResult:
        self._events.append("hub:rollback")
        return super().rollback(snap)


class RecoveryRunner:
    """Records every tag (into a shared event log) and serves a SCRIPTED lease.

    Models the REAL recovery transport: the same runner serves ``lease_observe``
    AND ``fw_wan_ip`` (what ``verify.verify`` reads) plus ``traceroute`` (hop-2).
    The lease queue is consumed one per ``lease_observe``/``fw_wan_ip`` so a test
    can script "APIPA persists" vs "recovered to a double-NAT private lease". The
    traceroute is empty (no hop-2) so ``classify_nat`` decides purely on the WAN
    IP — a private WAN -> Nat.DOUBLE -> Verdict.NOT_YET (the recovered state), an
    APIPA WAN -> Verdict.APIPA_ROLLBACK (recovery failed).
    """

    def __init__(self, leases: list[str], events: list[str]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._leases = list(leases)
        self._events = events

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            self._events.append(f"runner:{tag[0]}")
            if len(self._leases) > 1:
                return self._leases.pop(0)
            return self._leases[0] if self._leases else ""
        if tag and tag[0] == "dhcp_release":
            self._events.append("runner:dhcp_release")
        return ""


def test_rollback_with_persistent_apipa_release_reports_not_ok() -> None:
    """(a) A re-lease that leaves the downstream WAN STILL APIPA must report
    ok=False — a swallowed re-lease that did not bring the WAN back to a working
    non-APIPA lease has stranded the household, and reporting green would hide a
    dark network from the operator (no manual-recovery surfaced)."""
    from sanctum_cli.devices.intents import _DmzRollbackProvider

    events: list[str] = []
    hub = EventHub(events)
    # DMZ is currently engaged (a prior cutover). The re-lease NEVER clears APIPA.
    hub._v[DMZ_PATH] = "true"
    runner = RecoveryRunner(["169.254.10.5"], events)
    wrapped = _DmzRollbackProvider(hub, runner)

    snap = Snapshot(brand=hub.brand, taken_at="t", data={DMZ_PATH: "false"})
    result = wrapped.rollback(snap)

    # The inner rollback DID disable DMZ (the dangerous bit is off) ...
    assert hub.get(DMZ_PATH) == "false"
    # ... but the WAN never recovered, so the OVERALL rollback is NOT ok.
    assert result.ok is False
    assert "apipa" in result.detail.lower() or "did not" in result.detail.lower()


def test_rollback_reboots_before_release_then_verifies_double_nat() -> None:
    """(b) Engaging DMZ latched via a reboot, so disabling it must reboot too:
    the rollback disables DMZ, REBOOTS the hub, THEN re-leases DHCP, and only then
    verifies the WAN recovered to a working double-NAT lease before reporting ok."""
    from sanctum_cli.devices.intents import _DmzRollbackProvider

    events: list[str] = []
    hub = EventHub(events)
    hub._v[DMZ_PATH] = "true"
    # The downstream recovers to a private (double-NAT) lease — the expected post-
    # rollback state once DMZ is off and the WAN re-leases behind the hub's NAT.
    runner = RecoveryRunner(["192.168.2.20"], events)
    wrapped = _DmzRollbackProvider(hub, runner)

    snap = Snapshot(brand=hub.brand, taken_at="t", data={DMZ_PATH: "false"})
    result = wrapped.rollback(snap)

    # Recovered to a working double-NAT lease → ok=True.
    assert result.ok is True
    assert hub.get(DMZ_PATH) == "false"
    assert hub.reboot_calls == 1
    # Ordering: the hub reboot fired strictly BEFORE the downstream re-lease so the
    # DMZ-disable had latched before the router tried to pull its recovered lease.
    assert "hub:reboot" in events
    assert "runner:dhcp_release" in events
    assert events.index("hub:reboot") < events.index("runner:dhcp_release")


def test_rollback_when_inner_disable_fails_does_not_reboot_or_release() -> None:
    """If the inner DMZ-disable itself fails, the rollback surfaces that failure
    and must NOT reboot or re-lease on top of an un-disabled DMZ — there is nothing
    safe to recover to while the hub is still in Advanced DMZ."""
    from sanctum_cli.devices.intents import _DmzRollbackProvider

    events: list[str] = []

    class FailingRollbackHub(EventHub):
        def rollback(self, snap: Snapshot) -> OpResult:
            self._events.append("hub:rollback")
            return OpResult(ok=False, detail="hub rejected the DMZ-disable write")

    hub = FailingRollbackHub(events)
    hub._v[DMZ_PATH] = "true"
    runner = RecoveryRunner(["192.168.2.20"], events)
    wrapped = _DmzRollbackProvider(hub, runner)

    snap = Snapshot(brand=hub.brand, taken_at="t", data={DMZ_PATH: "false"})
    result = wrapped.rollback(snap)

    assert result.ok is False
    # No reboot, no re-lease on top of a still-engaged DMZ.
    assert hub.reboot_calls == 0
    assert not any(t and t[0] == "dhcp_release" for t in runner.calls)


# ── FIX (a-2): the ACTIVE box ops RIDE the post-reboot hub-dark window ─────────
#
# observe_lease already rides the dark window (FIX a). But the ACTIVE box ops — the
# wan_dhcp re-lease (a flip stage) and the rollback's dhcp_release — were single SSH
# shots: the instant the box's WAN→Tailscale was down during the 2-5 min hub-reboot
# window, the op's transport (RuntimeError out of the fail-closed runner) false-failed
# the stage / the rollback. On 2026-06-27 that left a "ROLLBACK FAILED, half-applied".
# These tests pin the dark-window ride (intents._ride_dark_window) driven by the pure
# flip.box_op_retry_decision: a box-op transport that fails N times THEN succeeds must
# be RIDDEN (retry, ultimately succeed, never false-fail); a box that NEVER returns by
# the bound must fail closed (raise/ok=False), bounded, never hang, never mask.
#
# The direct driver tests inject a monotonic-shaped fake clock + a no-op sleep against
# a REAL flaky box-op callable (no time-mocking of the impl); the e2e tests drive the
# REAL orchestrator / the REAL _DmzRollbackProvider with a flaky runner and the tiny
# real ride window from the autouse fixture.


def test_ride_dark_window_reachable_box_fires_once_no_sleep() -> None:
    """A reachable box (the op returns) fires exactly ONCE and never sleeps — the ride
    must add ZERO delay to the happy path (a healthy box is not needlessly waited on)."""
    from sanctum_cli.devices import intents

    calls = {"n": 0}

    def box_op() -> str:
        calls["n"] += 1
        return ""

    clock = _FakeClock([0.0, 1.0])
    sleeps: list[float] = []
    intents._ride_dark_window(
        box_op, op="wan_dhcp", timeout_s=480.0, poll_interval_s=15.0, now=clock, sleep=sleeps.append
    )
    assert calls["n"] == 1
    assert sleeps == []


def test_ride_dark_window_rides_transport_failures_then_succeeds() -> None:
    """THE HOSTILE SCENARIO: a box-op transport that times out 3x (box unreachable
    mid-reboot) THEN succeeds (the box returned) must be RIDDEN — the op retries and
    ultimately lands, returning normally (no false-fail). Re-fired once per tick, all
    elapsed (<480) inside the window. Real flaky callable + a real monotonic-shaped
    fake clock, no time-mocking of the impl."""
    from sanctum_cli.devices import intents

    calls = {"n": 0}

    def box_op() -> str:
        calls["n"] += 1
        if calls["n"] <= 3:  # the box is unreachable for the first 3 ticks of the window
            msg = "ssh: connect to host 100.68.36.16 port 22: Operation timed out"
            raise RuntimeError(msg)
        return ""  # the box came back from the hub reboot

    clock = _FakeClock([0.0, 60.0, 120.0, 180.0])  # all < the 480 s bound
    sleeps: list[float] = []
    # Returns normally (no _StageError) → the op ultimately landed.
    intents._ride_dark_window(
        box_op, op="wan_dhcp", timeout_s=480.0, poll_interval_s=15.0, now=clock, sleep=sleeps.append
    )
    assert calls["n"] == 4  # 3 transport failures + 1 success
    assert len(sleeps) == 3  # slept once per retry, never after the op landed


def test_ride_dark_window_box_never_returns_fails_closed_at_bound() -> None:
    """A box that NEVER returns by the bound fails CLOSED: the ride raises a _StageError
    at/after the deadline — bounded (not an infinite retry loop), never hangs, and never
    masks the failure as success."""
    from sanctum_cli.devices import intents

    def box_op() -> str:
        msg = "ssh: connect to host 100.68.36.16 port 22: Operation timed out"
        raise RuntimeError(msg)

    clock = _FakeClock([0.0, 200.0, 400.0, 600.0])  # crosses the 480 s bound on tick 3
    sleeps: list[float] = []
    with pytest.raises(intents._StageError) as ei:
        intents._ride_dark_window(
            box_op,
            op="dhcp_release",
            timeout_s=480.0,
            poll_interval_s=15.0,
            now=clock,
            sleep=sleeps.append,
        )
    low = str(ei.value).lower()
    assert "dhcp_release" in low
    assert "did not return" in low or "never reached" in low
    # Bounded: a handful of retries then gave up — never an infinite loop.
    assert 1 <= len(sleeps) <= 4


class FlakyWanDhcpRunner(FakeRunner):
    """A FakeRunner whose ``wan_dhcp`` op RAISES RuntimeError the first ``fail_times``
    calls (the box unreachable in the hub-reboot dark window) then succeeds — so a test
    can prove the wan_dhcp stage RIDES the window instead of false-failing on the first
    timed-out SSH. Every other tag (incl. the armored ``public`` lease) is unchanged."""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self.fail_times = fail_times
        self.wan_dhcp_attempts = 0

    def __call__(self, tag: tuple[str, ...]) -> str:
        if tag == ("wan_dhcp",):
            self.wan_dhcp_attempts += 1
            if self.wan_dhcp_attempts <= self.fail_times:
                msg = "ssh: box unreachable in the hub-reboot dark window"
                raise RuntimeError(msg)
            self.calls.append(tag)
            return ""
        return super().__call__(tag)


def test_wan_dhcp_stage_rides_dark_window_and_commits(tmp_path: Path) -> None:
    """E2E: the wan_dhcp re-lease that times out 2x (box mid-reboot) then succeeds is
    RIDDEN — the cutover does NOT false-fail/roll back, it commits. Drives the REAL
    orchestrator; only the box-op transport is flaky."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    runner = FlakyWanDhcpRunner(fail_times=2)
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is True  # rode the window → committed, did NOT false-fail
    assert runner.wan_dhcp_attempts == 3  # 2 transport failures + 1 success
    assert hub.rollback_calls == 0
    assert hub.get(DMZ_PATH) == "true"


class FlakyReleaseRecoveryRunner(RecoveryRunner):
    """A RecoveryRunner whose ``dhcp_release`` RAISES RuntimeError the first
    ``fail_times`` calls (the box unreachable in the rollback's POST-REBOOT dark
    window) then succeeds — modelling the box returning from the hub reboot. The
    scripted recovery lease is served on the reads as usual."""

    def __init__(self, leases: list[str], events: list[str], *, fail_times: int) -> None:
        super().__init__(leases, events)
        self.fail_times = fail_times
        self.release_attempts = 0

    def __call__(self, tag: tuple[str, ...]) -> str:
        if tag and tag[0] == "dhcp_release":
            self.release_attempts += 1
            if self.release_attempts <= self.fail_times:
                msg = "ssh: box unreachable in the rollback dark window"
                raise RuntimeError(msg)
        return super().__call__(tag)


def test_rollback_rides_dark_window_for_release_then_recovers_double_nat() -> None:
    """THE 06-27 INCIDENT, FIXED: the rollback's dhcp_release times out 2x (the box is
    mid-reboot after the latch-reboot) then succeeds — the rollback RIDES the window,
    the WAN recovers to a working double-NAT lease, and the rollback completes CLEANLY
    (ok=True) instead of the old "ROLLBACK FAILED, half-applied"."""
    from sanctum_cli.devices.intents import _DmzRollbackProvider

    events: list[str] = []
    hub = EventHub(events)
    hub._v[DMZ_PATH] = "true"  # a prior cutover engaged DMZ
    runner = FlakyReleaseRecoveryRunner(["192.168.2.20"], events, fail_times=2)
    wrapped = _DmzRollbackProvider(hub, runner)

    snap = Snapshot(brand=hub.brand, taken_at="t", data={DMZ_PATH: "false"})
    result = wrapped.rollback(snap)

    assert result.ok is True  # rode the dark window → recovered cleanly
    assert runner.release_attempts == 3  # 2 transport failures + 1 success
    assert hub.get(DMZ_PATH) == "false"  # DMZ disabled
    assert hub.reboot_calls == 1  # the latch-reboot still fired before the re-lease


def test_rollback_release_box_never_returns_reports_not_ok_with_manual_recovery() -> None:
    """A box that NEVER returns for the rollback re-lease fails CLOSED: the rollback
    reports ok=False with a manual-recovery instruction — it must never mask a truly
    dark box as a clean recovery. The inner DMZ-disable still ran, so the dangerous
    leaf is off."""
    from sanctum_cli.devices.intents import _DmzRollbackProvider

    events: list[str] = []
    hub = EventHub(events)
    hub._v[DMZ_PATH] = "true"
    # The box never returns from the reboot for the re-lease (always raises transport).
    runner = FlakyReleaseRecoveryRunner(["192.168.2.20"], events, fail_times=10_000)
    wrapped = _DmzRollbackProvider(hub, runner)

    snap = Snapshot(brand=hub.brand, taken_at="t", data={DMZ_PATH: "false"})
    result = wrapped.rollback(snap)

    assert result.ok is False  # fail closed — a dark box is never reported as recovered
    assert hub.get(DMZ_PATH) == "false"  # the dangerous DMZ leaf was still disabled
    low = result.detail.lower()
    assert "recover" in low or "manual" in low or "did not return" in low


# ── FIX-e: requires_slash32_armor=False — skip the armor stages + accept any prefix ─
#
# For a non-Bell ISP whose passthrough yields a NORMAL public lease (no /1 poison),
# the cutover must NOT deploy the self-healing /32 armor and the poison gate must
# accept a public lease of any prefix. These drive the FULL orchestrator with the
# flag off and assert: zero armor stage/install, a /24 public lease COMMITS, and the
# enable_dmz interlock passes even though the armor was never staged.


class NormalPublicRunner:
    """Serves a NORMAL public /24 lease (the typical non-Bell passthrough).

    ``lease_observe`` returns a public IP (→ classify "public"); the raw readback
    carries a /24 (NOT the Bell /32 armor) and a clean route table (no 0.0.0.0/1).
    The old /32-only poison gate would (wrongly) reject this perfectly healthy lease.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            return "24.150.33.7"
        if tag == ("wan_addr_cidr",):
            return "2: eth0    inet 24.150.33.7/24 brd 24.150.33.255 scope global eth0"
        if tag == ("wan_routes",):
            return "default via 24.150.33.1 dev eth0"
        return ""


def test_dmz_non_bell_skips_armor_and_commits_normal_public_lease(tmp_path: Path) -> None:
    """requires_slash32_armor=False: a /24 public lease COMMITS, and NEITHER armor
    stage runs — the cutover skips the /32 armor for an ISP that doesn't need it."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), NormalPublicRunner(), FakeArmor()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        requires_slash32_armor=False,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is True  # the /24 lease committed (old /32 gate would reject)
    # The armor was NEITHER staged NOR installed — both stages skipped.
    assert armor.staged == 0
    assert armor.installed == 0
    # DMZ still engaged + committed; no rollback.
    assert (DMZ_PATH, "true") in hub.set_calls
    assert hub.get(DMZ_PATH) == "true"
    assert hub.rollback_calls == 0


def test_dmz_non_bell_interlock_engages_without_armor_staged(tmp_path: Path) -> None:
    """The enable_dmz interlock must NOT block when armor is skipped (FIX-e): with the
    flag off the armor-staged precondition is satisfied by construction, so DMZ engages
    even though stage_armor never ran."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), NormalPublicRunner(), FakeArmor()
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        oob_probe=lambda: True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        requires_slash32_armor=False,
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is True
    assert armor.staged == 0  # the interlock did NOT require a stage that never ran
    assert (DMZ_PATH, "true") in hub.set_calls


def test_dmz_non_bell_dry_run_plan_omits_armor_steps() -> None:
    """The dry-run plan for a non-armor ISP omits the /32-armor steps (honest plan)."""
    from sanctum_cli.devices.intents import single_nat_dmz

    res = single_nat_dmz(
        FakeHub(), NormalPublicRunner(), FakeArmor(),
        apply=False, requires_slash32_armor=False,
    )
    plan_text = "\n".join(res.plan).lower()
    assert "armor" not in plan_text  # no /32-armor stage lines for this ISP
    assert "enable advanced dmz" in plan_text  # the DMZ engage step still shows


def test_dmz_bell_default_still_runs_armor(tmp_path: Path) -> None:
    """REGRESSION: the default (requires_slash32_armor=True) still stages + installs the
    armor and requires the /32 lease — the Bell behavior is untouched."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, runner, armor = FakeHub(), FakeRunner(), FakeArmor()  # FakeRunner serves /32
    res = single_nat_dmz(
        hub,
        runner,
        armor,
        apply=True,
        out_of_band_reachable=True,
        force=True,
        stage_verifiers=_all_pass_verifiers(),
        log_path=tmp_path / "audit.jsonl",
    )
    assert res.result is not None
    assert res.result.ok is True
    assert armor.staged == 1  # Bell still stages …
    assert armor.installed == 1  # … and installs the armor
