"""Pure single-NAT flip stage-machine — strings/enums in, decisions out.

The single-NAT (Bell Advanced DMZ + ``/32``) cutover is a multi-stage, attended
operation: preflight → put the hub's WAN into DHCP → enable Advanced DMZ →
reboot the hub → observe the new downstream lease → install the armor kit →
verify reachability → arm the watchdog. Each stage either succeeds (advance to
the next) or fails (the whole flip must unwind — ROLLBACK).

:mod:`sanctum_cli.devices.flip` is the PURE brain of that sequence, modelled on
the armor's ``lib/singlenat-eval.sh``: it takes captured strings/enums (which
stages are done, whether the last one succeeded, the observed WAN class, an
out-of-band-reachability precondition) and returns a *decision* — the next
stage, ``"ROLLBACK"``, a retry verdict, or a gate verdict. It performs **no
I/O**: no ssh, no reboot, no armor install, no clock. The I/O wrappers (the CLI
command, the sagemcom provider's ``reboot``/``set``, the Firewalla runner, the
armor installer) are mocked in their own tests; here we exercise only the
decision logic so a hostile fixture can drive every branch deterministically.

These tests are authored from the *consumer's* contract (the stage order the
cutover runbook actually performs, the WAN-class vocabulary the armor's
``classify_wan_ip`` already emits) rather than from the production module's
assumptions (Contracts at the Boundary: a test must not share the bug it is
meant to catch).
"""

from __future__ import annotations

import pytest

from sanctum_cli.devices import flip

# The canonical, ordered cutover stages the runbook performs. Derived from the
# corrected (post-06-26) attended single-NAT runbook + the armor README: preflight
# gate → WAN→DHCP → **STAGE the /32 armor while the LAN is still healthy** →
# Advanced DMZ → hub reboot → observe the downstream lease → confirm/install armor →
# verify reachability → arm the watchdog. The armor MUST be staged BEFORE DMZ
# engages + the hub reboots (FIX-2) so the box pulls Bell's poison /1 with the /32
# supersede already in place — never the un-armored window that collapsed the LAN on
# 06-26. Authored from the runbook order, NOT copied from the production tuple.
EXPECTED_ORDER = (
    "preflight",
    "wan_dhcp",
    "stage_armor",
    "enable_dmz",
    "hub_reboot",
    "observe_lease",
    "apply_armor",
    "verify",
    "arm",
)

ROLLBACK = "ROLLBACK"


# ── FLIP_STAGES: the ordered stage tuple ─────────────────────────────────────


def test_flip_stages_is_the_canonical_ordered_tuple() -> None:
    """FLIP_STAGES is the exact attended-cutover order, as a tuple (immutable)."""
    assert isinstance(flip.FLIP_STAGES, tuple)
    assert flip.FLIP_STAGES == EXPECTED_ORDER


def test_flip_stages_armor_before_dmz_engage_and_reboot() -> None:
    """FIX-2 (the ordering CONTRACT): the /32 armor is STAGED strictly BEFORE the
    DMZ engages AND before the hub reboots.

    On 2026-06-26 the armor was installed AFTER enable_dmz + hub_reboot, so the box
    pulled Bell's poison /1 lease un-armored and the LAN collapsed. The corrected
    machine stages the armor while the LAN is still healthy, so the /32 supersede is
    already in place the instant the post-reboot DMZ lease arrives. This pins the
    relative order — not just the field — so a future re-shuffle that re-opens the
    un-armored window fails here.
    """
    stages = flip.FLIP_STAGES
    assert "stage_armor" in stages, "the flip must have a pre-DMZ armor-staging stage"
    assert stages.index("stage_armor") < stages.index("enable_dmz")
    assert stages.index("stage_armor") < stages.index("hub_reboot")


# ── next_stage: the happy advance + the rollback fork ─────────────────────────


def test_next_stage_full_happy_order() -> None:
    """Walking the machine with last_ok=True at every step visits the stages in
    order and then terminates (None when all stages are done)."""
    done: list[str] = []
    visited: list[str] = []
    # At most len(stages)+1 iterations; the +1 proves it terminates cleanly.
    for _ in range(len(EXPECTED_ORDER) + 1):
        nxt = flip.next_stage(done, last_ok=True)
        if nxt is None:
            break
        assert nxt != ROLLBACK  # the happy path never forks to rollback
        visited.append(nxt)
        done.append(nxt)
    assert visited == list(EXPECTED_ORDER)
    # Every stage done → no next stage.
    assert flip.next_stage(list(EXPECTED_ORDER), last_ok=True) is None


def test_next_stage_first_stage_from_empty_done() -> None:
    """With nothing done and the (vacuous) last step ok, the first stage is next."""
    assert flip.next_stage([], last_ok=True) == EXPECTED_ORDER[0]


def test_next_stage_advances_one_at_a_time() -> None:
    """Each completed-prefix maps to exactly the following stage."""
    for i in range(len(EXPECTED_ORDER) - 1):
        done = list(EXPECTED_ORDER[: i + 1])
        assert flip.next_stage(done, last_ok=True) == EXPECTED_ORDER[i + 1]


@pytest.mark.parametrize("done_count", range(len(EXPECTED_ORDER)))
def test_next_stage_rollback_on_any_failed_stage(done_count: int) -> None:
    """A failed stage (last_ok=False) at ANY point forks to ROLLBACK, regardless
    of how far the sequence had progressed."""
    done = list(EXPECTED_ORDER[:done_count])
    assert flip.next_stage(done, last_ok=False) == ROLLBACK


def test_next_stage_rollback_even_when_all_done() -> None:
    """A failed final stage still rolls back — completion does not mask failure."""
    assert flip.next_stage(list(EXPECTED_ORDER), last_ok=False) == ROLLBACK


def test_next_stage_does_not_mutate_done_list() -> None:
    """next_stage is pure: it must not append to / mutate the caller's done list."""
    done = ["preflight", "wan_dhcp"]
    snapshot = list(done)
    flip.next_stage(done, last_ok=True)
    flip.next_stage(done, last_ok=False)
    assert done == snapshot


# ── should_retry_apipa: retry a stuck/empty lease exactly once ────────────────


@pytest.mark.parametrize("observed", ["apipa", "none"])
def test_should_retry_apipa_true_on_first_attempt_for_bad_lease(observed: str) -> None:
    """An APIPA (169.254.x) or empty/no lease on attempt 1 is worth one retry —
    the downstream router often grabs a stale/self-assigned address on the first
    DHCP after the hub reboot, and a single re-lease usually clears it."""
    assert flip.should_retry_apipa(observed, attempt=1) is True


@pytest.mark.parametrize("observed", ["apipa", "none"])
def test_should_retry_apipa_false_on_second_attempt(observed: str) -> None:
    """One retry only: a bad lease that survives attempt 1 is a real failure, not
    a transient — do NOT loop forever re-leasing a genuinely broken cutover."""
    assert flip.should_retry_apipa(observed, attempt=2) is False


@pytest.mark.parametrize("observed", ["public", "double_nat"])
def test_should_retry_apipa_false_for_non_bad_class_on_first_attempt(observed: str) -> None:
    """A public (single-NAT win) or double_nat lease is NOT a retry case — public
    is success, double_nat is a definite verdict the caller handles elsewhere."""
    assert flip.should_retry_apipa(observed, attempt=1) is False


def test_should_retry_apipa_false_on_higher_attempts() -> None:
    """Any attempt beyond the first never retries, for any class."""
    for observed in ("apipa", "none", "public", "double_nat"):
        assert flip.should_retry_apipa(observed, attempt=3) is False


# ── classify_wan_ip: the armor's 4-way WAN vocabulary, ported pure ────────────
#
# These expectations are authored from the armor kit's OWN ``classify_wan_ip``
# (sanctum-singlenat-armor/lib/singlenat-eval.sh) case arms — the consumer of
# this vocabulary — NOT from the Python producer's body. The Python classifier
# MUST agree token-for-token with the shell so the CLI cutover and the on-box
# self-heal speak the same WAN classes (Contracts at the Boundary). The shell:
#   ""|0.0.0.0                                   -> none
#   169.254.*                                    -> apipa
#   192.168.*|10.*|172.16-31.*                   -> double_nat
#   *                                            -> public


@pytest.mark.parametrize("wan_ip", ["", "0.0.0.0"])
def test_classify_wan_ip_none_for_empty_or_unspecified(wan_ip: str) -> None:
    """An empty/no lease or 0.0.0.0 is ``none`` — the router pulled nothing."""
    assert flip.classify_wan_ip(wan_ip) == "none"


def test_classify_wan_ip_none_for_actual_none() -> None:
    """A Python ``None`` (the runner read returned nothing) is also ``none`` —
    the orchestrator passes the raw read straight through without pre-empting."""
    assert flip.classify_wan_ip(None) == "none"


@pytest.mark.parametrize("wan_ip", ["169.254.0.1", "169.254.213.108", "169.254.255.255"])
def test_classify_wan_ip_apipa_for_link_local(wan_ip: str) -> None:
    """A 169.254.x self-assigned address is ``apipa`` — DHCP failed (retryable)."""
    assert flip.classify_wan_ip(wan_ip) == "apipa"


@pytest.mark.parametrize(
    "wan_ip",
    ["192.168.2.10", "10.0.0.5", "172.16.0.1", "172.20.1.2", "172.31.255.254"],
)
def test_classify_wan_ip_double_nat_for_rfc1918(wan_ip: str) -> None:
    """An RFC1918 private lease (192.168.x / 10.x / 172.16-31.x) is ``double_nat``
    — the hub handed out its own LAN address, the cutover did NOT pass through."""
    assert flip.classify_wan_ip(wan_ip) == "double_nat"


@pytest.mark.parametrize("wan_ip", ["203.0.113.7", "74.14.213.108", "172.15.0.1", "172.32.0.1"])
def test_classify_wan_ip_public_for_routable(wan_ip: str) -> None:
    """A routable public IP is ``public`` — the single-NAT win. Note 172.15 and
    172.32 are NOT in the 172.16-31 private block, so they are public (the exact
    boundary the shell case arms draw)."""
    assert flip.classify_wan_ip(wan_ip) == "public"


# ── gate_ok: the precondition guard ──────────────────────────────────────────


def test_gate_ok_true_when_out_of_band_reachable() -> None:
    """The flip may only start when there is an out-of-band path to recover the
    hub if the cutover drops the WAN (the attended-only safety precondition)."""
    assert flip.gate_ok(out_of_band_reachable=True) is True


def test_gate_ok_refuses_without_out_of_band_path() -> None:
    """No out-of-band reachability → refuse: a flip that drops the WAN with no
    recovery path could strand the household dark with no way back in."""
    assert flip.gate_ok(out_of_band_reachable=False) is False


# ── evaluate_interlock: the fail-closed 3-precondition gate (FIX-3) ───────────
#
# The 06-26 strand engaged DMZ with NO armor staged and only a LAN-bound recovery
# channel that died with the LAN. The prevent-interlock refuses to engage DMZ
# unless, AT THE MOMENT of the op, ALL THREE hold: (a) the out-of-band channel is
# proven-live, (b) the /32 armor is staged, (c) a rollback baseline is staged. The
# brain is a pure AND-gate (no I/O); these author the truth table directly.


def test_interlocked_stages_includes_enable_dmz() -> None:
    """The DMZ-engage stage is the one guarded by the prevent-interlock."""
    assert "enable_dmz" in flip.INTERLOCKED_STAGES


def test_evaluate_interlock_engages_only_when_all_three_hold() -> None:
    """Engage iff OOB-live AND armor-staged AND rollback-staged — the AND-gate."""
    d = flip.evaluate_interlock(
        oob_channel_live=True, armor_staged=True, rollback_staged=True
    )
    assert d.engage is True
    assert d.reason  # carries a legible "all proven" reason


@pytest.mark.parametrize(
    ("oob", "armor", "rb"),
    [
        (False, True, True),
        (True, False, True),
        (True, True, False),
        (False, False, True),
        (False, True, False),
        (True, False, False),
        (False, False, False),
    ],
)
def test_evaluate_interlock_refuses_when_any_precondition_absent(
    oob: bool, armor: bool, rb: bool
) -> None:
    """Fail-closed: if ANY of the three preconditions is absent, refuse to engage.

    This is the gate that, had it existed on 06-26, would have refused the cutover
    (no armor staged + only a LAN-bound OOB) instead of engaging DMZ un-armored.
    """
    d = flip.evaluate_interlock(
        oob_channel_live=oob, armor_staged=armor, rollback_staged=rb
    )
    assert d.engage is False
    assert d.reason  # a refusal must say WHY


def test_evaluate_interlock_reason_names_each_missing_precondition() -> None:
    """A refusal with ALL three down names each missing precondition (legible)."""
    d = flip.evaluate_interlock(
        oob_channel_live=False, armor_staged=False, rollback_staged=False
    )
    assert d.engage is False
    low = d.reason.lower()
    assert "out-of-band" in low or "out of band" in low or "tailscale" in low
    assert "armor" in low
    assert "rollback" in low
