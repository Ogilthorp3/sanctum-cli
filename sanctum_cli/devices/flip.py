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
    172.3[0-1].*`` — i.e. ``192.168.0.0/16``, ``10.0.0.0/8`` and the ``172.16.0.0``
    through ``172.31.255.255`` block — string-prefix style so a malformed octet is
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
