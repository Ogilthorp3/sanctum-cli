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
3. ``enable_dmz``    — engage Bell Advanced DMZ (the bridge-equivalent single-NAT).
4. ``hub_reboot``    — reboot the hub so the new WAN/DMZ config takes effect.
5. ``observe_lease`` — read the downstream router's new WAN lease + classify it.
6. ``apply_armor``   — install the single-NAT armor kit (self-healing /32 + MTU).
7. ``verify``        — confirm real-site reachability through the new single NAT.
8. ``arm``           — bootstrap the watchdog/sentinel so drift self-heals.

The contract mirrors the rails (:mod:`sanctum_cli.devices.rails`): any stage
that fails unwinds the whole flip. :func:`next_stage` returns the next stage on
success, :data:`ROLLBACK` on a failed stage, and ``None`` once every stage is
done — so the driver loop is a pure fold over (done-so-far, last-outcome).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# The canonical, ordered single-NAT cutover stages. A tuple so it is immutable
# and order-defining; the driver walks it via :func:`next_stage`.
FLIP_STAGES: tuple[str, ...] = (
    "preflight",
    "wan_dhcp",
    "enable_dmz",
    "hub_reboot",
    "observe_lease",
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
