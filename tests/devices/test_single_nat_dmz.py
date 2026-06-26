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
from sanctum_cli.devices.sagemcom import _MUTATED_XPATHS, _SAFE_BASELINE

if TYPE_CHECKING:
    from pathlib import Path

# The Bell Advanced-DMZ leaf the cutover engages (the brand's DMZ capability op).
DMZ_PATH = "Device/Services/BellNetworkCfg/AdvancedDMZ"


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
            return CapabilityOp(path=DMZ_PATH, engaged="on")
        return None

    def snapshot(self, scope: str | None = None) -> Snapshot:
        # Best-effort capture of what the firmware surfaces (None reads dropped) …
        data = {path: value for path, value in self._v.items() if value is not None}
        # … then the REAL provider's hard guarantee: every leaf the cutover MUTATES
        # gets a restorable baseline even when unread. Derived from the PRODUCTION
        # tuple so this fake fails until DMZ is actually in _MUTATED_XPATHS.
        for path in _MUTATED_XPATHS:
            data.setdefault(path, _SAFE_BASELINE)
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

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        if tag and tag[0] in ("fw_wan_ip", "lease_observe"):
            if len(self._leases) > 1:
                return self._leases.pop(0)
            return self._leases[0] if self._leases else ""
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
    """Mock single-NAT armor installer: records the install, never touches disk."""

    def __init__(self, *, ok: bool = True) -> None:
        self.installed = 0
        self._ok = ok

    def install(self) -> OpResult:
        self.installed += 1
        if not self._ok:
            return OpResult(ok=False, detail="armor install failed")
        return OpResult(ok=True, detail="armor installed")


def _all_pass_verifiers() -> dict[str, object]:
    """A stage→verifier map where every stage's probe passes (the happy path)."""
    return {stage: (lambda: True) for stage in flip.FLIP_STAGES}


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
    assert (DMZ_PATH, "on") in hub.set_calls
    assert hub.get(DMZ_PATH) == "on"
    # It rebooted the hub for the new WAN/DMZ config to take.
    assert hub.reboot_calls == 1
    # It installed the armor kit.
    assert armor.installed == 1
    # A successful flip never rolls back.
    assert hub.rollback_calls == 0
    # It drove the runner (WAN→DHCP + observe-lease).
    assert runner.calls


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
    assert hub.get(DMZ_PATH) == "off"
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
    so rollback re-issues ``set(DMZ_PATH, "off")``.

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
    assert (DMZ_PATH, "on") in hub.set_calls
    # … and despite the firmware never surfacing the leaf at snapshot time, the
    # rollback drove it back OFF (proof the snapshot carried the guaranteed
    # baseline and rollback re-issued the restore write).
    assert hub.rollback_calls == 1
    assert (DMZ_PATH, "off") in hub.set_calls
    assert hub.get(DMZ_PATH) == "off"


def test_dmz_reboot_failure_rolls_back(tmp_path: Path) -> None:
    """A stage whose I/O RAISES (the hub rejects the reboot) trips rollback too —
    a raised change is the worst case the rails exist to catch."""
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
    assert hub.reboot_calls == 1  # it tried
    assert hub.rollback_calls == 1  # and unwound
    assert hub.get(DMZ_PATH) == "off"
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
    assert hub.get(DMZ_PATH) == "off"


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
    assert hub.get(DMZ_PATH) == "on"
    assert armor.installed == 1


def test_observe_lease_persistent_apipa_rolls_back_after_one_retry(tmp_path: Path) -> None:
    """A 169.254.x APIPA that SURVIVES the single re-lease is a real failure: the
    stage raises so the rails unwind — DMZ rolled back to off, ok=False — and only
    ONE re-lease was attempted (no infinite re-lease loop)."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    # APIPA on every read — the re-lease never clears it.
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
    # Exactly ONE re-lease was attempted (one retry only — never loop forever).
    assert runner.releases_between_first_two_reads() == 1
    assert runner.lease_reads() == 2  # observe, retry-observe — then give up
    # Persistent APIPA → the flip unwound: DMZ disabled, household not left dark.
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "off"
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
    assert hub.get(DMZ_PATH) == "off"


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
    assert hub.get(DMZ_PATH) == "on"


def test_observe_lease_double_nat_first_read_fires_no_release_but_fails_stage_verify(
    tmp_path: Path,
) -> None:
    """A double_nat (RFC1918) lease is NOT retryable — re-leasing a hub-handed
    private address would not help. The observe_lease stage proceeds WITHOUT a
    re-lease (double_nat is not in the retry set); the CLI's real observe-lease
    verifier is what then rejects double_nat. Here we model that with a failing
    observe_lease verifier and assert: zero re-leases, then rollback."""
    from sanctum_cli.devices.intents import single_nat_dmz

    hub, armor = FakeHub(), FakeArmor()
    runner = ScriptedLeaseRunner(["192.168.2.10"])  # hub's own LAN → double_nat
    verifiers = _all_pass_verifiers()
    # The real CLI verifier rejects a non-SINGLE lease; model that rejection here.
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
    assert res.result is not None
    assert res.result.ok is False
    # double_nat is NOT retryable → the stage fired no re-lease of its own.
    assert not any(t and t[0] == "dhcp_release" for t in runner.calls[: runner.lease_reads() + 1])
    assert hub.rollback_calls == 1
    assert hub.get(DMZ_PATH) == "off"


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
