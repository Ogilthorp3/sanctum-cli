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
# attended single-NAT runbook + the armor README (preflight gate → WAN→DHCP →
# Advanced DMZ → hub reboot → observe the downstream lease → install armor →
# verify reachability → arm the watchdog), NOT copied from the production tuple.
EXPECTED_ORDER = (
    "preflight",
    "wan_dhcp",
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


# ── gate_ok: the precondition guard ──────────────────────────────────────────


def test_gate_ok_true_when_out_of_band_reachable() -> None:
    """The flip may only start when there is an out-of-band path to recover the
    hub if the cutover drops the WAN (the attended-only safety precondition)."""
    assert flip.gate_ok(out_of_band_reachable=True) is True


def test_gate_ok_refuses_without_out_of_band_path() -> None:
    """No out-of-band reachability → refuse: a flip that drops the WAN with no
    recovery path could strand the household dark with no way back in."""
    assert flip.gate_ok(out_of_band_reachable=False) is False
