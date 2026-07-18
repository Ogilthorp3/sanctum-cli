"""Pure decision brain for the single-NAT (Bell Advanced DMZ + ``/32``) flip.

The single-NAT cutover is a multi-stage, attended operation that briefly drops
the whole household's internet, so its sequencing must be deterministic, fully
testable, and impossible to fire by accident. This module is that sequencer's
*brain* — modelled on the armor kit's ``lib/singlenat-eval.sh``: every function
takes captured strings/enums (which stages are done, whether the last stage
succeeded, the observed downstream WAN class, an out-of-band-reachability
precondition) and returns a *decision*. It performs **no I/O** — no ssh, no
hub reboot, no DHCP, no armor install, no clock — so it stays import-cheap and
every branch is exercisable with a hostile fixture.

The I/O lives at the boundary (the CLI command, the
:class:`~sanctum_cli.devices.sagemcom.SagemcomHubProvider`'s ``set``/``reboot``,
the Firewalla runner that reads the downstream lease, the armor installer);
each of those is tested against its own mock. This module is the seam they all
consult to decide *what to do next*, never *how to do it*.

The stages, in order (:data:`FLIP_STAGES`):

1. ``preflight``     — sanity + the gate (see :func:`gate_ok`) before touching anything.
2. ``wan_dhcp``      — put the hub's WAN into DHCP/PPPoE-passthrough so the
                       downstream router can obtain the public lease.
3. ``stage_armor``   — install + verify the self-healing ``/32`` armor on the box
                       WHILE THE LAN IS STILL HEALTHY (FIX-2), so the supersede is
                       already in place when the post-DMZ lease arrives. The armor
                       MUST be staged before DMZ engages — on 2026-06-26 it landed
                       AFTER the reboot, so the box pulled Bell's poison ``/1``
                       un-armored and the LAN collapsed.
4. ``enable_dmz``    — engage Bell Advanced DMZ (the bridge-equivalent single-NAT).
5. ``hub_reboot``    — reboot the hub so the new WAN/DMZ config takes effect.
6. ``observe_lease`` — read the downstream router's new WAN lease + classify it.
7. ``apply_armor``   — post-cutover: confirm the armor came up HEALTHY now single-NAT
                       is live (the deploy already landed at ``stage_armor``).
8. ``verify``        — confirm real-site reachability through the new single NAT.
9. ``arm``           — bootstrap the watchdog/sentinel so drift self-heals.

The contract mirrors the rails (:mod:`sanctum_cli.devices.rails`): any stage
that fails unwinds the whole flip. :func:`next_stage` returns the next stage on
success, :data:`ROLLBACK` on a failed stage, and ``None`` once every stage is
done — so the driver loop is a pure fold over (done-so-far, last-outcome).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# The canonical, ordered single-NAT cutover stages. A tuple so it is immutable
# and order-defining; the driver walks it via :func:`next_stage`.
FLIP_STAGES: tuple[str, ...] = (
    "preflight",
    "wan_dhcp",
    # FIX-2: the /32 armor is STAGED (deployed + structurally verified) while the LAN
    # is still healthy, BEFORE Advanced DMZ engages and the hub reboots — so the box
    # never pulls Bell's poison /1 lease un-armored (the 06-26 LAN-collapse window).
    "stage_armor",
    "enable_dmz",
    "hub_reboot",
    "observe_lease",
    # Post-cutover: the deploy already landed at ``stage_armor``; this confirms the
    # armor came up HEALTHY now that single-NAT is actually live.
    "apply_armor",
    "verify",
    "arm",
)

# The sentinel :func:`next_stage` returns when the last stage failed: the driver
# must unwind the flip rather than advance. A distinct string (not a stage name,
# not None) so a caller can never confuse "roll back" with "advance" or "done".
ROLLBACK = "ROLLBACK"

# A bad downstream WAN lease worth exactly one re-lease retry: a self-assigned
# APIPA (169.254.x) or an empty/no lease the router often grabs on the first
# DHCP after the hub reboot. Matches the armor's ``classify_wan_ip`` vocabulary
# ("public" | "double_nat" | "apipa" | "none") so the two never drift.
_RETRYABLE_LEASE_CLASSES = frozenset({"apipa", "none"})


def next_stage(done: Sequence[str], last_ok: bool) -> str | None:
    """Decide the next flip stage, ``ROLLBACK``, or ``None`` (all done).

    A pure fold over the flip's progress:

    * ``last_ok=False`` → the stage that just ran failed; the whole flip must
      unwind, so return :data:`ROLLBACK` regardless of how far it had progressed
      (a failed *final* stage still rolls back — completion never masks failure).
    * otherwise return the first stage in :data:`FLIP_STAGES` not present in
      ``done`` (the next step to run), or ``None`` when every stage is done.

    ``done`` is the ordered list of stages already completed successfully. This
    function never mutates it and never performs I/O — the driver appends the
    just-run stage to its own list and calls again.
    """
    if not last_ok:
        return ROLLBACK
    completed = set(done)
    for stage in FLIP_STAGES:
        if stage not in completed:
            return stage
    return None


def should_retry_apipa(observed_class: str, attempt: int) -> bool:
    """Should we re-lease after a bad downstream WAN lease? One retry only.

    True iff this is the FIRST attempt (``attempt == 1``) AND the observed WAN
    class is a retryable bad lease — ``"apipa"`` (169.254.x self-assigned) or
    ``"none"`` (empty/no lease). The downstream router frequently grabs a
    stale/self-assigned address on the first DHCP right after the hub reboot,
    and a single re-lease usually clears it.

    A bad lease that survives attempt 1 is treated as a real failure, not a
    transient: any ``attempt`` beyond the first returns False so the driver
    never loops forever re-leasing a genuinely broken cutover. A ``"public"``
    (single-NAT win) or ``"double_nat"`` (definite verdict handled elsewhere)
    class is never a retry case. ``observed_class`` uses the armor's
    ``classify_wan_ip`` vocabulary so the two stay in lockstep.
    """
    return attempt == 1 and observed_class in _RETRYABLE_LEASE_CLASSES


# ── settle/poll brain (FIX a): wait THROUGH the normal post-reboot hub-dark window
#
# Engaging Advanced DMZ + rebooting the hub leaves the downstream WAN dark for the
# normal 2-5 min hub-reboot window (apipa/none) before the public lease arrives.
# The old single-shot observe_lease raced that window and false-failed a cutover
# that would have succeeded. :func:`settle_poll_decision` is the PURE decision the
# bounded poll consults each tick: it distinguishes "still settling within the
# window" from "hard fail past the bound" — the (a) contract — and NEVER masks a
# genuine failure (``double_nat`` fails at once; a transient that survives the whole
# window fails at the bound). It takes ``elapsed_s`` (no clock of its own) so every
# branch is exercised with a hostile fixture.


@dataclass(frozen=True)
class SettleDecision:
    """A single settle-poll tick's verdict.

    ``action`` is one of ``"settled_ok"`` (the public single-NAT lease is up —
    stop), ``"keep_polling"`` (still in the normal hub-dark window — wait + nudge),
    or ``"hard_fail"`` (a genuine failure — unwind the flip). ``re_lease`` is True
    only when the driver should fire one DHCP re-lease nudge before the next tick.
    ``reason`` is a legible explanation for the audit log / operator.
    """

    action: str
    re_lease: bool
    reason: str


# Past this downstream WAN class the poll STOPS immediately — it is a definite
# verdict no amount of waiting/re-leasing can change. "double_nat" means Advanced
# DMZ never took (the hub handed its own LAN address through), so the poll must
# fail at once rather than wait out the window on a cutover that already lost.
_DEFINITE_FAILURE_LEASE_CLASSES = frozenset({"double_nat"})


def settle_poll_decision(
    observed_class: str, *, elapsed_s: float, timeout_s: float
) -> SettleDecision:
    """Decide one settle-poll tick: settled_ok / keep_polling / hard_fail. Pure.

    The single function that encodes the (a) "distinguish settling-within-window
    from hard-fail-past-timeout" contract, and it never masks a genuine failure:

    * ``"public"`` → ``settled_ok`` (the single-NAT win; no re-lease).
    * ``"double_nat"`` → ``hard_fail`` AT ONCE (DMZ did not take — never waited on).
    * a transient (:data:`_RETRYABLE_LEASE_CLASSES` — ``apipa``/``none``) while
      ``elapsed_s < timeout_s`` → ``keep_polling`` with a re-lease nudge (the normal
      hub-dark window the poll waits through).
    * a transient at/after the bound (``elapsed_s >= timeout_s``) → ``hard_fail``
      (the WAN genuinely never came up — surfaced only AFTER the window proves it is
      not merely settling). The ``>=`` makes the boundary fail closed.

    Any other (unrecognized) class fails closed — we never commit on a class we
    cannot prove is the single-NAT win.
    """
    if observed_class == "public":
        return SettleDecision(
            action="settled_ok",
            re_lease=False,
            reason="downstream WAN is public — single-NAT lease is up",
        )
    if observed_class in _DEFINITE_FAILURE_LEASE_CLASSES:
        return SettleDecision(
            action="hard_fail",
            re_lease=False,
            reason="downstream WAN is double-NAT — Advanced DMZ did not take",
        )
    if observed_class in _RETRYABLE_LEASE_CLASSES:
        if elapsed_s < timeout_s:
            return SettleDecision(
                action="keep_polling",
                re_lease=True,
                reason=(
                    f"still settling ({elapsed_s:.0f}/{timeout_s:.0f}s) — "
                    "normal hub-dark window"
                ),
            )
        return SettleDecision(
            action="hard_fail",
            re_lease=False,
            reason=(
                f"downstream WAN still {observed_class} after {timeout_s:.0f}s — "
                "WAN never came up"
            ),
        )
    return SettleDecision(
        action="hard_fail",
        re_lease=False,
        reason=f"downstream WAN class {observed_class!r} is unrecognized — failing closed",
    )


# ── box-op dark-window ride (FIX a-2): an ACTIVE post-reboot box op must RIDE the
#    hub-reboot dark window, not single-shot through it ──────────────────────────
#
# ``settle_poll_decision`` rides the dark window for the OBSERVE (a lease-class read).
# But the two ACTIVE box ops — the ``wan_dhcp`` re-lease and the rollback's
# ``dhcp_release`` — were single SSH attempts: the instant the box's WAN→Tailscale was
# down during the 2-5 min hub-reboot window, the op's transport timed out and the stage
# / the rollback false-failed (the 2026-06-27 "ROLLBACK FAILED, half-applied"). The
# fix rides those ops through the window with the SAME bounded-poll machinery, but the
# signal is the box's REACHABILITY (did the op's transport succeed) rather than a lease
# class. :func:`box_op_retry_decision` is the PURE decision the ride consults AFTER a
# transport failure — retry while inside the window, give up (fail closed) past it —
# and it takes ``elapsed_s`` (no clock of its own) so every branch is exercised with a
# hostile fixture.


@dataclass(frozen=True)
class BoxOpRetryDecision:
    """One box-op dark-window tick's verdict, consulted AFTER the op's transport FAILED.

    ``action`` is ``"retry"`` (still inside the hub-reboot window — the box has not come
    back yet; wait + re-fire the op) or ``"give_up"`` (the bound elapsed — the box never
    returned; fail closed). ``reason`` is a legible explanation for the audit log /
    operator. There is no ``"succeeded"`` action: a transport SUCCESS is the ride loop's
    own stop signal (the op landed), so the decision is only ever consulted on a failure.
    """

    action: str
    reason: str


def box_op_retry_decision(*, op: str, elapsed_s: float, timeout_s: float) -> BoxOpRetryDecision:
    """Decide a box-op dark-window tick after a transport failure: retry / give_up. Pure.

    The active box op (the ``wan_dhcp`` re-lease, the rollback's ``dhcp_release``) reaches
    the box ONLY over the link the cutover is bouncing, so during the 2-5 min hub-reboot
    window its SSH transport fails. That is NOT a genuine op failure — it is the box being
    transiently unreachable — so:

    * ``elapsed_s < timeout_s`` → ``retry`` (still inside the window; the box has not come
      back yet — wait + re-fire the op).
    * ``elapsed_s >= timeout_s`` → ``give_up`` (the box never returned by the bound — fail
      closed: a genuinely dead box must surface, never hang forever, never be masked). The
      ``>=`` makes the boundary fail closed, exactly like :func:`settle_poll_decision`.

    ``op`` is the op's name (``"wan_dhcp"`` / ``"dhcp_release"``) woven into ``reason`` so
    the audit log names which box op was riding the window.
    """
    if elapsed_s < timeout_s:
        return BoxOpRetryDecision(
            action="retry",
            reason=(
                f"box op {op!r} transport failed at {elapsed_s:.0f}/{timeout_s:.0f}s — "
                "box unreachable in the hub-reboot window; retrying"
            ),
        )
    return BoxOpRetryDecision(
        action="give_up",
        reason=(
            f"box op {op!r} did not return within {timeout_s:.0f}s — "
            "the box never came back from the hub-reboot window"
        ),
    )


def classify_wan_ip(wan_ip: str | None) -> str:
    """Classify a downstream WAN IPv4 into the armor's 4-way vocabulary.

    A faithful Python port of the armor kit's ``classify_wan_ip``
    (``sanctum-singlenat-armor/lib/singlenat-eval.sh``) so the CLI cutover and the
    on-box self-heal classify a lease IDENTICALLY (Contracts at the Boundary — the
    two must never drift). Returns exactly one of:

    * ``"none"``       — empty/no lease or ``0.0.0.0`` (the router pulled nothing);
                         also ``None`` (the runner read returned nothing).
    * ``"apipa"``      — a ``169.254.x`` self-assigned link-local (DHCP failed).
    * ``"double_nat"`` — an RFC1918 private lease (``192.168.x`` / ``10.x`` /
                         ``172.16-31.x``): the hub handed out its own LAN address,
                         the single-NAT passthrough did NOT take.
    * ``"public"``     — anything else: a routable public IP (the single-NAT win).

    The ``"apipa"`` and ``"none"`` classes are the retryable ones
    (:data:`_RETRYABLE_LEASE_CLASSES`); :func:`should_retry_apipa` consumes this
    same vocabulary to decide the single re-lease retry. The shell's case-arm
    boundaries are reproduced exactly: ``172.15`` and ``172.32`` are public (only
    ``172.16``-``172.31`` are private).
    """
    if not wan_ip or wan_ip == "0.0.0.0":
        return "none"
    if wan_ip.startswith("169.254."):
        return "apipa"
    if _is_rfc1918(wan_ip):
        return "double_nat"
    return "public"


def _is_rfc1918(wan_ip: str) -> bool:
    """True iff ``wan_ip`` is in an RFC1918 private block, matching the shell case.

    Mirrors the armor shell's glob arm ``192.168.*|10.*|172.1[6-9].*|172.2[0-9].*|
    172.3[0-1].*`` — i.e. the ``192.168/16``, ``10/8`` and ``172.16``-``172.31`` private
    blocks — string-prefix style so a malformed octet is
    treated by the same textual rule the shell uses (no ``ipaddress`` parse that
    could disagree on an edge the shell would still glob-match).
    """
    if wan_ip.startswith(("192.168.", "10.")):
        return True
    if wan_ip.startswith("172."):
        try:
            second = int(wan_ip.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


# ── netmask/route poison gate (FIX c): a "public" lease can still be poisoned ───
#
# Bell's Advanced DMZ hands the WAN a PUBLIC IP carrying a /1 netmask whose on-link
# 0.0.0.0/1 route overlaps the 1-127.x LAN and collapses forwarding. The /32 armor's
# supersede pins the WAN to /32 + an on-link gateway. ``classify_wan_ip`` reads only
# the bare IPv4 (``lease_observe`` strips the prefix), so a "public" lease that is
# actually carrying the poison — the /32 armor NOT holding — would commit GREEN on a
# dead LAN. This is the exact 2026-06-26 condition. :func:`evaluate_wan_poison` is
# the PURE gate the observe_lease seam consults before committing a "public" lease:
# committable IFF the armor is provably holding (WAN pinned to /32 AND no 0.0.0.0/1
# route). Authored from the consumer's real artifact (`ip -4 -o addr show` /
# `ip -4 route show`) — a different source than the producer's classify.

# The /1 split route Bell's DMZ installs (overlaps every 0.x-127.x LAN). Matches
# detect._BELL_DMZ_WAN_NET ("0.0.0.0/1").
_BELL_POISON_ROUTE = "0.0.0.0/1"
# A WAN address carrying a /1 netmask IS the poison (not the armor's /32).
_BELL_POISON_PREFIX = 1
# The armor pins the WAN to /32 (+ on-link gateway) — the only committable state.
_ARMOR_WAN_PREFIX = 32
# Captures the prefix length of every `inet <ipv4>/<prefix>` in `ip addr` output.
_INET_CIDR_RE = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})\b")


@dataclass(frozen=True)
class PoisonVerdict:
    """The poison gate's verdict: ``committable`` plus a legible ``reason``.

    ``committable`` is True only when the /32 armor is PROVABLY holding (the WAN is
    pinned to /32 AND no 0.0.0.0/1 poison route is present). On a refusal it is False
    and ``reason`` names each failing condition so the operator/audit sees WHY the
    cutover refused to commit a poisoned-but-public lease.
    """

    committable: bool
    reason: str


def parse_wan_prefixes(addr_show: str | None) -> list[int]:
    """Every IPv4 prefix length in `ip -4 -o addr show` output (e.g. ``[32]``).

    Pure parser of the consumer's real artifact (the prefix ``lease_observe``
    strips). Empty/None input yields ``[]`` so a missing readback fails closed
    downstream (no /32 → cannot prove the armor holds).
    """
    if not addr_show:
        return []
    return [int(m.group(2)) for m in _INET_CIDR_RE.finditer(addr_show)]


def poison_route_present(route_show: str | None) -> bool:
    """True iff the Bell ``0.0.0.0/1`` poison route is present in `ip route` output.

    Matches a line that is exactly ``0.0.0.0/1`` or begins ``0.0.0.0/1 `` (the
    ``0.0.0.0/1 via … dev …`` form), so a route table that merely *mentions* the
    string elsewhere cannot false-trip it. Empty/None input is "not present".
    """
    if not route_show:
        return False
    return any(
        s == _BELL_POISON_ROUTE or s.startswith(_BELL_POISON_ROUTE + " ")
        for s in (ln.strip() for ln in route_show.splitlines())
    )


def evaluate_wan_poison(
    addr_show: str | None,
    route_show: str | None,
    *,
    requires_slash32_armor: bool = True,
) -> PoisonVerdict:
    """Is a "public" WAN lease SAFE to commit, or is it carrying Bell's /1 poison?

    ``requires_slash32_armor`` is the per-playbook gate (FIX-e). It is True ONLY for
    the Bell Advanced-DMZ cutover, whose public lease can still be carrying the /1
    poison if the ``/32`` armor did not hold; for every other ISP it is False (the
    passthrough yields a NORMAL public lease — there is no /1 poison to supersede).

    When ``requires_slash32_armor`` is True, committable IFF BOTH hold (the /32 armor
    is provably in place):

    * the WAN is pinned to ``/32`` (the armor's address-supersede is holding), AND
    * no ``0.0.0.0/1`` route is present (the armor's route-supersede is holding).

    A ``/1`` (or any non-/32) prefix, a present ``0.0.0.0/1`` route, OR an
    unparseable/empty readback all fail closed — we never commit unless we can PROVE
    the armor holds.

    When ``requires_slash32_armor`` is False, the ``/32`` requirement is dropped: a
    healthy public lease of ANY prefix is committable. The Bell /1-poison guards are
    STILL enforced (a present ``0.0.0.0/1`` route or a ``/1`` netmask still refuses) —
    they can never legitimately appear off a non-Bell WAN, so checking them costs
    nothing and keeps the gate fail-closed against a surprise poison signal.

    Pure: the I/O that reads ``addr_show``/``route_show`` lives at the boundary (the
    runner's raw-readback tags).
    """
    prefixes = parse_wan_prefixes(addr_show)
    failures: list[str] = []
    if poison_route_present(route_show):
        failures.append(f"Bell {_BELL_POISON_ROUTE} poison route present (overlaps the LAN)")
    if _BELL_POISON_PREFIX in prefixes:
        failures.append(
            f"WAN address carries a /{_BELL_POISON_PREFIX} netmask — "
            "Bell's poison, not the armor's /32"
        )
    if requires_slash32_armor and _ARMOR_WAN_PREFIX not in prefixes:
        failures.append(
            f"WAN not pinned to /{_ARMOR_WAN_PREFIX} — the /32 armor is NOT holding "
            f"(prefixes={prefixes or 'none'})"
        )
    if failures:
        return PoisonVerdict(
            committable=False,
            reason="WAN poison check FAILED — " + "; ".join(failures),
        )
    if requires_slash32_armor:
        ok_reason = (
            f"WAN poison check OK — pinned to /{_ARMOR_WAN_PREFIX}, "
            f"no {_BELL_POISON_ROUTE} route"
        )
    else:
        ok_reason = (
            "WAN lease OK — healthy public lease, no Bell /1 poison "
            "(/32 armor not required for this ISP)"
        )
    return PoisonVerdict(committable=True, reason=ok_reason)


# ── box preflight (FIX-f): the box must be CAPABLE of the cutover's ops ────────
#
# The cutover's ACTIVE box ops (``wan_dhcp`` re-lease, the rollback's ``dhcp_release``)
# run ``sudo dhclient <wan>`` over the key-SSH transport. Two box-side capabilities
# the apply path silently ASSUMED — and that a fresh operator's box may not have:
#   * passwordless sudo — without it ``sudo`` blocks on a password prompt with no TTY,
#     so the op hangs until the SSH ConnectTimeout and FALSE-FAILS mid-cutover; and
#   * a real ``dhclient`` — without it the release/renew cannot run at all.
# :func:`evaluate_box_preflight` is the PURE gate the apply path consults BEFORE any
# mutation, so a box missing either capability refuses up front (fail-closed, clear
# message) instead of stranding the WAN. It takes the two captured booleans (the I/O
# probe lives at the boundary — ``system.firewalla_box_preflight`` over the existing
# SSH) so every branch is exercised with a hostile fixture.


@dataclass(frozen=True)
class PreflightDecision:
    """The box-preflight verdict: ``ok`` plus a legible ``reason``.

    ``ok`` is True only when the box can run the cutover's ``sudo dhclient`` ops —
    passwordless sudo AND a ``dhclient`` present. On a refusal it is False and
    ``reason`` names each missing capability so the operator (and the audit log) sees
    exactly WHY the cutover refused before it touched anything.
    """

    ok: bool
    reason: str


def evaluate_box_preflight(
    *, passwordless_sudo: bool, dhclient_present: bool
) -> PreflightDecision:
    """Fail-closed AND-gate: can the box run the cutover's ``sudo dhclient`` ops? Pure.

    Ready iff BOTH hold:

    * ``passwordless_sudo`` — ``sudo -n true`` over the SSH succeeds, so the
      release/renew ``sudo`` calls won't block on a TTY-less password prompt.
    * ``dhclient_present`` — a real DHCP client exists, so the re-lease can run.

    Either absent → ``ok=False`` with a reason naming what is missing. Pure so every
    branch is exercised with a hostile fixture; the I/O probe that produces these
    booleans lives at the boundary (:func:`sanctum_cli.net.system.firewalla_box_preflight`).
    """
    missing: list[str] = []
    if not passwordless_sudo:
        missing.append("passwordless sudo is not available on the box (`sudo -n true` failed)")
    if not dhclient_present:
        missing.append("no DHCP client found on the box (`dhclient` not on PATH)")
    if missing:
        return PreflightDecision(
            ok=False,
            reason="box preflight FAILED — " + "; ".join(missing),
        )
    return PreflightDecision(
        ok=True,
        reason="box preflight OK — passwordless sudo + dhclient present",
    )


def gate_ok(out_of_band_reachable: bool) -> bool:
    """The flip's start precondition: an out-of-band recovery path must exist.

    The cutover briefly drops the WAN, and a misstep can leave the hub
    unreachable over the very link the flip is changing. The attended-only
    safety contract requires an out-of-band path (e.g. the Mini jump host on a
    separate link, or physical access) to recover the hub if the cutover
    strands it — so without that path the gate refuses and the flip never
    starts. Pure boolean precondition; the caller decides *how* it learned
    reachability.
    """
    return out_of_band_reachable


# ── the prevent-interlock (FIX-3): a fail-closed gate AT the DMZ-engage moment ─
#
# ``gate_ok`` is the cheap START precondition checked once. The interlock is the
# AUTHORITATIVE gate evaluated AT THE MOMENT DMZ is engaged (the irreversible step
# that hands the WAN to Bell's DMZ), so it reflects state *right then* — a channel
# can die between preflight and engage. On 2026-06-26 the cutover engaged DMZ with
# NO armor staged and only a LAN-bound recovery channel that died with the LAN; the
# interlock refuses to engage unless all three of those holes are closed.

# The stage(s) whose mutation is guarded by :func:`evaluate_interlock`. ``enable_dmz``
# is the irreversible "hand the WAN to Bell's DMZ" step — the one that must never
# fire unless the three preconditions hold at that instant.
INTERLOCKED_STAGES: frozenset[str] = frozenset({"enable_dmz"})


@dataclass(frozen=True)
class InterlockDecision:
    """The prevent-interlock's verdict: ``engage`` plus a legible ``reason``.

    ``engage`` is True only when every precondition holds; on a refusal it is False
    and ``reason`` names each missing precondition so the operator (and the audit
    log) sees exactly WHY the cutover refused to engage DMZ.
    """

    engage: bool
    reason: str


def evaluate_interlock(
    *, oob_channel_live: bool, armor_staged: bool, rollback_staged: bool
) -> InterlockDecision:
    """Fail-closed AND-gate: may we engage DMZ *right now*? Pure, no I/O.

    Engage iff ALL THREE hold at the moment of the op:

    * ``oob_channel_live`` — the LAN-INDEPENDENT out-of-band channel (the
      Tailscale-on-box path) is proven-live, so a cutover that drops the LAN can
      still be recovered. (06-26: the LAN-bound channel died with the LAN.)
    * ``armor_staged`` — the self-healing ``/32`` armor is already staged on the
      box, so Bell's poison ``/1`` lease is superseded the instant it arrives.
      (06-26: armor was installed AFTER the reboot — too late.)
    * ``rollback_staged`` — a rollback baseline is captured, so a failed engage can
      be unwound.

    Any precondition absent → ``engage=False`` with a reason naming what is missing.
    The brain is pure so every branch is exercised with a hostile fixture; the I/O
    probes that produce these booleans live at the boundary (the CLI / the
    :mod:`sanctum_cli.devices.interlock` Tailscale probe / the snapshot rails).
    """
    missing: list[str] = []
    if not oob_channel_live:
        missing.append("out-of-band channel not proven-live (Tailscale-on-box)")
    if not armor_staged:
        missing.append("/32 armor not staged on the box")
    if not rollback_staged:
        missing.append("rollback baseline not staged")
    if missing:
        return InterlockDecision(
            engage=False,
            reason="refused to engage DMZ — " + "; ".join(missing),
        )
    return InterlockDecision(
        engage=True,
        reason="interlock OK — OOB channel proven-live, /32 armor staged, rollback staged",
    )
