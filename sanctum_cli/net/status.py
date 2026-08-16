"""Sanctum Net Status — the pure half of the one-glance network roll-up.

``sanctum net status`` collapses what today takes 3-4 separate commands (+ an SSH
to the Firewalla) into ONE read-only pane. This module is the *pure* assembler:
:func:`build_status_report` takes the already-probed subsystem value objects —
posture (``heal``), never-strand spine, heal-daemon health, identity (``link``),
NAT topology (``detect``), and the Firewalla trust-guardian heartbeat — and maps
them onto per-row statuses plus a single OVERALL verdict. All impure probing lives
in the thin ``net status`` handler in ``commands.net`` (each probe wrapped so a
failure degrades that row to UNKNOWN, never crashes the pane).

Verdict doctrine:

* **GREEN** — nothing is down and nothing needs attention.
* **ATTENTION** — a non-urgent issue that works now but is at-risk (a drifted
  posture that would strand on a foreign LAN, a rotating MAC that is reachable for
  now). The node is fine *right now*.
* **DEGRADED** — a *continuous-protection* layer is DOWN: the heal daemon is not
  loaded, the never-strand spine is down, the identity is quarantined, or the
  Firewalla guardian heartbeat is stale. These are the "your safety net has a hole"
  states, so they dominate the roll-up.

UNKNOWN rows (a probe that failed or could not verify) are *fail-open* for the
overall verdict — an unread subsystem is never treated as proof of an outage — so
a single flaky probe cannot false-alarm the whole pane to DEGRADED.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sanctum_cli.net.heal import PostureDiagnosis
    from sanctum_cli.net.link import IdentityDiagnosis
    from sanctum_cli.net.types import TopologyReport


class RowStatus(Enum):
    """Per-subsystem roll-up status. Ordered worst-last for reduction is NOT relied
    upon — the overall verdict is computed from explicit membership, not ordinal."""

    OK = "ok"
    ATTENTION = "attention"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SpineInfo:
    """The never-strand spine: is the tailnet and/or TB5 bridge up? Both ``None``
    is never used — the CLI reads these from ifconfig (``heal._spine_from_ifconfig``),
    which always yields two booleans; feed ``None`` (the whole object) to mark the
    spine probe as failed → UNKNOWN row."""

    on_tailnet: bool
    tb5_up: bool


@dataclass(frozen=True)
class DaemonInfo:
    """The ``com.sanctum.net-heal`` LaunchDaemon health. ``loaded`` is whether
    launchctl reports it present; ``last_result`` is the last-known heartbeat
    outcome token (``healed`` / ``reverted`` / ``noop`` / a status word), or ``None``
    when no heartbeat is readable.

    ``age_seconds`` is how old the last heartbeat is (parsed from its ISO timestamp),
    or ``None`` when no timestamp could be read; ``fresh`` is whether that age is
    within the freshness window (``None`` when unknown). A daemon that launchctl
    reports "loaded" but whose heartbeat is hours old is WEDGED — not guarding — so
    the pure row gates on freshness the way the guardian row does, rather than
    trusting ``loaded`` alone."""

    loaded: bool
    last_result: str | None
    age_seconds: int | None = None
    fresh: bool | None = None


@dataclass(frozen=True)
class GuardianInfo:
    """The Firewalla trust-guardian heartbeat (best-effort, optional).

    ``reachable`` is whether we could read the heartbeat at all over the existing
    Firewalla SSH seam; ``fresh`` is whether its age is under the freshness window
    (``None`` when unknown); ``age_seconds`` is the heartbeat age (``None`` unknown).
    An unreachable guardian is UNKNOWN — never DEGRADED — because it is optional."""

    reachable: bool
    fresh: bool | None
    age_seconds: int | None


@dataclass(frozen=True)
class StatusRow:
    """One rendered subsystem row: a label, its roll-up status, and a one-line detail."""

    label: str
    status: RowStatus
    detail: str


@dataclass(frozen=True)
class StatusReport:
    """The assembled one-glance roll-up: the ordered rows + the single OVERALL verdict."""

    rows: tuple[StatusRow, ...]
    overall: str


# Identity verdicts that mean the node's on-network identity is actively broken
# (a continuous-protection outage) vs merely at-risk (works now, ATTENTION).
_IDENTITY_DOWN = {"IDENTITY_QUARANTINED"}
_IDENTITY_ATTENTION = {"IDENTITY_ROTATING"}
_IDENTITY_OK = {"IDENTITY_STABLE"}

# Posture verdicts that are at-risk (work now / would-strand later) vs OK. Everything
# that is neither HEALTHY nor a clean UNVERIFIED read is treated as ATTENTION (the
# node needs a heal but is not a protection-layer outage on its own).
_POSTURE_OK = {"HEALTHY"}
_POSTURE_UNVERIFIED = {"UNVERIFIED"}


def _posture_row(posture: PostureDiagnosis | None) -> StatusRow:
    if posture is None:
        return StatusRow("Posture", RowStatus.UNKNOWN, "probe unavailable")
    verdict = posture.verdict
    if verdict in _POSTURE_UNVERIFIED:
        return StatusRow("Posture", RowStatus.UNKNOWN, f"{verdict} — {posture.detail}")
    p = posture.posture
    reach = (
        "reachable"
        if p.gateway_reachable
        else ("dead" if p.gateway_reachable is False else "unknown")
    )
    detail = (
        f"{verdict} · {p.iface or '-'} · {p.config_method or '-'} · "
        f"ip {p.ip or '-'} · gw {p.gateway or '-'} ({reach})"
    )
    status = RowStatus.OK if verdict in _POSTURE_OK else RowStatus.ATTENTION
    return StatusRow("Posture", status, detail)


def _spine_row(spine: SpineInfo | None) -> StatusRow:
    if spine is None:
        return StatusRow("Spine", RowStatus.UNKNOWN, "probe unavailable")
    detail = (
        f"{'tailnet ✓' if spine.on_tailnet else 'tailnet ✗'} · "
        f"{'TB5 ✓' if spine.tb5_up else 'TB5 ✗'}"
    )
    # Never-strand: either leg alive keeps an out-of-band path, so the spine is OK.
    up = spine.on_tailnet or spine.tb5_up
    return StatusRow("Spine", RowStatus.OK if up else RowStatus.DOWN, detail)


def _daemon_row(daemon: DaemonInfo | None) -> StatusRow:
    if daemon is None:
        return StatusRow("Heal daemon", RowStatus.UNKNOWN, "probe unavailable")
    last = f"last: {daemon.last_result}" if daemon.last_result else "no heartbeat yet"
    # 1. Not loaded at all → the continuous-protection layer is DOWN.
    if not daemon.loaded:
        return StatusRow(
            "Heal daemon",
            RowStatus.DOWN,
            f"com.sanctum.net-heal NOT loaded · {last} · run: sanctum net heal --install",
        )
    hb = (daemon.last_result or "").lstrip()
    # 2. STOP token — the no-loop cap was hit and auto-heal is PAUSED. The daemon is
    #    loaded and heart-beating, but it has stopped protecting → DOWN (a wedge the
    #    old "last: STOP on an OK row" hid). Also cover the not-found ERROR breadcrumb.
    if hb.startswith("STOP") or hb.startswith("ERROR"):
        return StatusRow(
            "Heal daemon",
            RowStatus.DOWN,
            f"com.sanctum.net-heal loaded but auto-heal PAUSED · {last}",
        )
    # 3. DISABLED kill-switch — an INTENTIONAL off (fail-safe), not a wedge: at-risk
    #    (no protection while off) but deliberate → ATTENTION, surfaced (not benign).
    if hb.startswith("DISABLED"):
        return StatusRow(
            "Heal daemon",
            RowStatus.ATTENTION,
            f"com.sanctum.net-heal loaded but kill-switch ON · {last}",
        )
    # 4. Loaded + heart-beating, but the last heartbeat is STALE (older than the
    #    freshness window) → the daemon is WEDGED / not firing on its interval. This
    #    is the must-fix: loaded=True no longer reads OK unconditionally.
    if daemon.fresh is False:
        age = f"{daemon.age_seconds}s ago" if daemon.age_seconds is not None else "age unknown"
        return StatusRow(
            "Heal daemon",
            RowStatus.DOWN,
            f"com.sanctum.net-heal loaded but heartbeat stale ({age}) · {last}",
        )
    # 5. Loaded and fresh (or freshness unknown — fail-open, an unread age is not
    #    proof of a wedge). It is running / guarding → OK, even if the last cycle
    #    reverted/noop'd (those are normal per-cycle outcomes, not an outage).
    return StatusRow("Heal daemon", RowStatus.OK, f"com.sanctum.net-heal loaded · {last}")


def _identity_row(identity: IdentityDiagnosis | None) -> StatusRow:
    if identity is None:
        return StatusRow("Identity", RowStatus.UNKNOWN, "probe unavailable")
    verdict = identity.verdict
    detail = f"{verdict} · {identity.detail}"
    if verdict in _IDENTITY_DOWN:
        status = RowStatus.DOWN
    elif verdict in _IDENTITY_ATTENTION:
        status = RowStatus.ATTENTION
    elif verdict in _IDENTITY_OK:
        status = RowStatus.OK
    else:  # IDENTITY_UNVERIFIED (or any unknown verdict) — could not verify.
        status = RowStatus.UNKNOWN
    return StatusRow("Identity", status, detail)


def _topology_row(topology: TopologyReport | None) -> StatusRow:
    if topology is None:
        return StatusRow("Topology", RowStatus.UNKNOWN, "probe unavailable")
    nat = topology.nat.value.upper()
    isp = topology.isp or "-"
    gw = topology.gateway_ip or "-"
    detail = f"{nat} NAT · isp {isp} · gw {gw}"
    # Topology is informational, not a protection layer: SINGLE/CGNAT are OK,
    # DOUBLE is ATTENTION (optimizable), UNKNOWN is UNKNOWN. It never forces DEGRADED.
    nat_value = topology.nat.value
    if nat_value == "double":
        status = RowStatus.ATTENTION
    elif nat_value == "unknown":
        status = RowStatus.UNKNOWN
    else:
        status = RowStatus.OK
    return StatusRow("Topology", status, detail)


def _guardian_row(guardian: GuardianInfo | None) -> StatusRow:
    if guardian is None:
        return StatusRow("Guardian", RowStatus.UNKNOWN, "probe unavailable")
    if not guardian.reachable:
        # Best-effort / optional: an unreachable guardian is UNKNOWN, never DOWN.
        return StatusRow(
            "Guardian", RowStatus.UNKNOWN, "Firewalla unreachable / no key (best-effort)"
        )
    age = f"{guardian.age_seconds}s ago" if guardian.age_seconds is not None else "age unknown"
    if guardian.fresh:
        return StatusRow("Guardian", RowStatus.OK, f"trust-guardian heartbeat fresh ({age})")
    return StatusRow("Guardian", RowStatus.DOWN, f"trust-guardian heartbeat STALE ({age})")


def build_status_report(
    *,
    posture: PostureDiagnosis | None,
    spine: SpineInfo | None,
    daemon: DaemonInfo | None,
    identity: IdentityDiagnosis | None,
    topology: TopologyReport | None,
    guardian: GuardianInfo | None,
) -> StatusReport:
    """PURE: assemble the six subsystem inputs into a roll-up report + OVERALL verdict.

    Each ``None`` input marks a *failed probe* → that row renders UNKNOWN (fail-open;
    the pane never crashes on a missing subsystem). The OVERALL verdict is:

    * **DEGRADED** if any *continuous-protection* row is ``DOWN`` (spine down, heal
      daemon not loaded, identity quarantined, guardian heartbeat stale),
    * else **ATTENTION** if any row is ``ATTENTION`` (at-risk but working now),
    * else **GREEN**.

    UNKNOWN rows never move the needle for the overall verdict — an unread subsystem
    is not proof of an outage, so a single flaky probe can't false-alarm the pane.
    """
    rows = (
        _posture_row(posture),
        _spine_row(spine),
        _daemon_row(daemon),
        _identity_row(identity),
        _topology_row(topology),
        _guardian_row(guardian),
    )
    statuses = {row.status for row in rows}
    if RowStatus.DOWN in statuses:
        overall = "DEGRADED"
    elif RowStatus.ATTENTION in statuses:
        overall = "ATTENTION"
    else:
        overall = "GREEN"
    return StatusReport(rows=rows, overall=overall)
