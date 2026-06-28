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


# ── settle_poll_decision: distinguish "still settling" from "hard fail" (FIX a) ─
#
# After the hub reboot the downstream WAN is normally DARK for 2-5 min (apipa/none)
# before the public lease arrives. The old single-shot observe_lease raced that
# window and FALSE-FAILED a cutover that would have succeeded. settle_poll_decision
# is the PURE brain (no clock — elapsed is passed in) the bounded poll consults:
#   - "public"                    -> settled_ok (the single-NAT win)
#   - "double_nat"                -> hard_fail at once (DMZ did not take; waiting cannot fix)
#   - apipa/none AND elapsed<bound -> keep_polling (the normal hub-dark window)
#   - apipa/none AND elapsed>=bound -> hard_fail (the WAN genuinely never came up)
# These author the truth table directly from that contract (Contracts at the
# Boundary: a /1-poisoned lease, a hub still dark mid-window, a transient past the
# bound are the hostile inputs).


def test_settle_poll_public_is_settled_ok_no_relelease() -> None:
    """A public lease is the single-NAT win: stop the poll, no re-lease."""
    d = flip.settle_poll_decision("public", elapsed_s=5.0, timeout_s=360.0)
    assert d.action == "settled_ok"
    assert d.re_lease is False


def test_settle_poll_double_nat_is_immediate_hard_fail_never_waited_on() -> None:
    """double_nat is a DEFINITE failure — DMZ did not take; waiting/re-leasing cannot
    fix it, so it hard-fails AT ONCE (well within the window) and is never masked."""
    d = flip.settle_poll_decision("double_nat", elapsed_s=1.0, timeout_s=360.0)
    assert d.action == "hard_fail"
    assert d.re_lease is False
    assert "double" in d.reason.lower()


@pytest.mark.parametrize("observed", ["apipa", "none"])
def test_settle_poll_transient_within_window_keeps_polling(observed: str) -> None:
    """The 06-26 hostile input: the hub is dark (apipa/none) MID-window. This must
    NOT fail — it keeps polling and nudges a re-lease (the normal settling window)."""
    d = flip.settle_poll_decision(observed, elapsed_s=30.0, timeout_s=360.0)
    assert d.action == "keep_polling"
    assert d.re_lease is True


@pytest.mark.parametrize("observed", ["apipa", "none"])
def test_settle_poll_transient_at_or_past_bound_hard_fails(observed: str) -> None:
    """A transient that SURVIVES the whole window is a genuine failure surfaced only
    AFTER the window proves it is not merely settling. Boundary elapsed==timeout
    fails closed (>=) so the window can never be over-trusted."""
    at_bound = flip.settle_poll_decision(observed, elapsed_s=360.0, timeout_s=360.0)
    assert at_bound.action == "hard_fail"
    assert at_bound.re_lease is False
    past_bound = flip.settle_poll_decision(observed, elapsed_s=400.0, timeout_s=360.0)
    assert past_bound.action == "hard_fail"


# ── box_op_retry_decision: ride the post-reboot box-op dark window (FIX a-2) ──────
#
# observe_lease already rides the dark window (settle_poll_decision). But the ACTIVE
# box ops — the wan_dhcp re-lease and the rollback's dhcp_release — were single SSH
# shots: the instant the box's WAN→Tailscale was down (the 2-5 min hub-reboot window)
# the op's transport timed out and the stage/rollback false-failed (the 06-27
# "rollback half-applied" incident). box_op_retry_decision is the PURE brain (no clock
# — elapsed is passed in) the bounded ride consults AFTER a transport failure: retry
# while inside the window, give up (fail closed) past the bound. Same >=-fail-closed
# boundary as settle_poll_decision; the hostile inputs are a box still dark mid-window
# and a box that never returns by the bound.


def test_box_op_retry_within_window_retries() -> None:
    """A box-op transport failure INSIDE the hub-reboot window → retry: the box has not
    come back from the reboot yet, this is NOT a genuine op failure."""
    d = flip.box_op_retry_decision(op="wan_dhcp", elapsed_s=60.0, timeout_s=480.0)
    assert d.action == "retry"
    assert "wan_dhcp" in d.reason


def test_box_op_retry_at_or_past_bound_gives_up_fail_closed() -> None:
    """A box that NEVER returns by the bound is a genuine failure — give up (fail
    closed), never hang forever, never mask. Boundary elapsed==timeout gives up (>=)
    so the window can never be over-trusted (exactly like settle_poll_decision)."""
    at_bound = flip.box_op_retry_decision(op="dhcp_release", elapsed_s=480.0, timeout_s=480.0)
    assert at_bound.action == "give_up"
    assert "dhcp_release" in at_bound.reason
    past_bound = flip.box_op_retry_decision(op="dhcp_release", elapsed_s=600.0, timeout_s=480.0)
    assert past_bound.action == "give_up"


# ── evaluate_wan_poison: refuse a "public" lease still carrying Bell's /1 (FIX c) ─
#
# Bell's Advanced DMZ hands the WAN a PUBLIC IP with a /1 netmask whose 0.0.0.0/1
# on-link route overlaps the 1-127.x LAN and collapses forwarding. The /32 armor's
# supersede pins the WAN to /32 + an on-link gateway. observe_lease only reads the
# bare IPv4 (it strips the prefix), so a "public" lease that is actually poisoned
# would commit green on a dead LAN — the exact 2026-06-26 condition. evaluate_wan_poison
# is the PURE gate: committable IFF the WAN is pinned to /32 AND no 0.0.0.0/1 route
# is present. Authored from the consumer's real artifact (`ip -4 -o addr show` /
# `ip -4 route show`), the hostile inputs being the poisoned readbacks.


def test_evaluate_wan_poison_healthy_armored_is_committable() -> None:
    """The armored success state: a /32 WAN + a clean route table (no 0.0.0.0/1)."""
    addr = "2: eth0    inet 24.150.33.7/32 brd 24.150.33.7 scope global eth0"
    routes = "default via 10.0.0.1 dev eth0\n24.150.33.7 dev eth0 scope link"
    v = flip.evaluate_wan_poison(addr, routes)
    assert v.committable is True


def test_evaluate_wan_poison_06_26_condition_refuses() -> None:
    """HOSTILE 06-26: a public IP with a /1 netmask AND a 0.0.0.0/1 poison route ->
    NOT committable; the reason names both the route and the /1."""
    addr = "2: eth0    inet 24.150.33.7/1 brd 127.255.255.255 scope global eth0"
    routes = "default via 10.111.0.1 dev eth0\n0.0.0.0/1 via 10.111.0.1 dev eth0"
    v = flip.evaluate_wan_poison(addr, routes)
    assert v.committable is False
    low = v.reason.lower()
    assert "0.0.0.0/1" in v.reason
    assert "/1" in v.reason
    assert "fail" in low


def test_evaluate_wan_poison_armor_addr_but_poison_route_survived_refuses() -> None:
    """HOSTILE: WAN pinned to /32 but the 0.0.0.0/1 route survived (route-supersede
    failed) -> not committable (a half-holding armor must not commit)."""
    addr = "2: eth0    inet 24.150.33.7/32 scope global eth0"
    routes = "default via 10.0.0.1 dev eth0\n0.0.0.0/1 via 10.0.0.1 dev eth0"
    v = flip.evaluate_wan_poison(addr, routes)
    assert v.committable is False


def test_evaluate_wan_poison_one_netmask_route_hidden_refuses() -> None:
    """HOSTILE: the address carries a /1 but the route table HIDES the 0.0.0.0/1 line
    (addr-only poison) -> still refuses on the /1 alone."""
    addr = "2: eth0    inet 24.150.33.7/1 scope global eth0"
    routes = "default via 10.0.0.1 dev eth0"
    v = flip.evaluate_wan_poison(addr, routes)
    assert v.committable is False


def test_evaluate_wan_poison_transient_both_prefixes_refuses() -> None:
    """HOSTILE: a transient where BOTH a /32 and a /1 are present -> any /1 fails."""
    addr = "2: eth0    inet 24.150.33.7/32 scope global eth0\n    inet 24.150.33.7/1 scope global eth0"
    routes = "default via 10.0.0.1 dev eth0"
    v = flip.evaluate_wan_poison(addr, routes)
    assert v.committable is False


@pytest.mark.parametrize("addr", ["", "<no inet>", None])
def test_evaluate_wan_poison_unparseable_readback_refuses(addr: str | None) -> None:
    """HOSTILE: a garbage/empty readback cannot PROVE the /32 armor holds -> refuse
    (fail-closed: we never commit unless we can prove the armor is holding)."""
    v = flip.evaluate_wan_poison(addr, "default via 10.0.0.1 dev eth0")
    assert v.committable is False


# ── FIX-e: requires_slash32_armor=False accepts a normal public lease (non-Bell) ─
#
# Bell's Advanced DMZ is the only method whose public lease can hide the /1 poison, so
# the /32 requirement is gated per-playbook. For every other ISP a healthy public lease
# of ANY prefix is committable — but the Bell /1-poison guards stay enforced (they can
# never legitimately appear off a non-Bell WAN; checking them costs nothing).


def test_evaluate_wan_poison_non_bell_accepts_normal_public_prefix() -> None:
    """A normal public /24 lease (the typical non-Bell passthrough) is committable
    when the playbook does NOT require the /32 armor — the old /32-only gate would
    have (wrongly) rejected this perfectly healthy lease."""
    addr = "2: eth0    inet 24.150.33.7/24 brd 24.150.33.255 scope global eth0"
    routes = "default via 24.150.33.1 dev eth0"
    # The default (Bell) gate rejects a non-/32 lease …
    assert flip.evaluate_wan_poison(addr, routes).committable is False
    # … but with the per-playbook flag off, the same healthy public lease commits.
    v = flip.evaluate_wan_poison(addr, routes, requires_slash32_armor=False)
    assert v.committable is True
    assert "/32 armor not required" in v.reason


@pytest.mark.parametrize("prefix", [16, 24, 30])
def test_evaluate_wan_poison_non_bell_accepts_any_prefix(prefix: int) -> None:
    """ANY public prefix is accepted for a non-armor ISP (not just /32)."""
    addr = f"2: eth0    inet 198.51.100.5/{prefix} scope global eth0"
    v = flip.evaluate_wan_poison(addr, "default via 198.51.100.1 dev eth0",
                                 requires_slash32_armor=False)
    assert v.committable is True


def test_evaluate_wan_poison_non_bell_still_refuses_poison_route() -> None:
    """Even with the /32 requirement OFF, a surprise 0.0.0.0/1 poison route STILL
    refuses — the relaxed gate never opens the door to the Bell /1 collapse."""
    addr = "2: eth0    inet 24.150.33.7/24 scope global eth0"
    routes = "default via 10.111.0.1 dev eth0\n0.0.0.0/1 via 10.111.0.1 dev eth0"
    v = flip.evaluate_wan_poison(addr, routes, requires_slash32_armor=False)
    assert v.committable is False
    assert "0.0.0.0/1" in v.reason


def test_evaluate_wan_poison_non_bell_still_refuses_slash1_netmask() -> None:
    """A /1 netmask STILL refuses even with the requirement off — it is the poison
    itself, never a legitimate non-Bell lease."""
    addr = "2: eth0    inet 24.150.33.7/1 scope global eth0"
    v = flip.evaluate_wan_poison(addr, "default via 10.0.0.1 dev eth0",
                                 requires_slash32_armor=False)
    assert v.committable is False


def test_evaluate_wan_poison_bell_default_unchanged_requires_slash32() -> None:
    """REGRESSION: the default (requires_slash32_armor=True) still demands /32 — a
    non-/32 Bell lease is refused exactly as before (the FIX-c behavior is intact)."""
    addr = "2: eth0    inet 24.150.33.7/24 scope global eth0"
    assert flip.evaluate_wan_poison(addr, "default via 10.0.0.1 dev eth0").committable is False


# ── FIX-f: the pure box-preflight gate (passwordless sudo + dhclient) ──────────


def test_evaluate_box_preflight_ready_when_both_present() -> None:
    """Passwordless sudo AND a dhclient → ready (the box can run the re-lease ops)."""
    d = flip.evaluate_box_preflight(passwordless_sudo=True, dhclient_present=True)
    assert d.ok is True


def test_evaluate_box_preflight_refuses_without_passwordless_sudo() -> None:
    """No passwordless sudo → refuse (the sudo dhclient op would hang on a TTY-less
    password prompt and false-fail mid-cutover). The reason names the missing piece."""
    d = flip.evaluate_box_preflight(passwordless_sudo=False, dhclient_present=True)
    assert d.ok is False
    assert "sudo" in d.reason.lower()


def test_evaluate_box_preflight_refuses_without_dhclient() -> None:
    """No dhclient → refuse (the re-lease cannot run at all)."""
    d = flip.evaluate_box_preflight(passwordless_sudo=True, dhclient_present=False)
    assert d.ok is False
    assert "dhclient" in d.reason.lower()


def test_evaluate_box_preflight_names_both_when_both_missing() -> None:
    """Both missing → refuse, and the reason names BOTH so the operator fixes both."""
    d = flip.evaluate_box_preflight(passwordless_sudo=False, dhclient_present=False)
    assert d.ok is False
    low = d.reason.lower()
    assert "sudo" in low
    assert "dhclient" in low
