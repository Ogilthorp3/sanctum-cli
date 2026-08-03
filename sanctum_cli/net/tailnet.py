"""Sanctum Tailnet — the pure half of the ``sanctum tailnet doctor`` roll-up.

``sanctum tailnet doctor`` collapses "is the tailnet actually usable?" into ONE
read-only pane. This module is the *pure* assembler + classifiers: given the
already-probed subsystem values — never-strand spine, peer reachability, API
credential health, ACL drift, and trifecta custody — it maps them onto per-row
statuses plus a single OVERALL verdict. All impure probing (ifconfig, the
``tailscale`` CLI, TCP connects, the Tailscale API, the keychain) lives in the
thin handler in ``commands.tailnet``, each behind a seam the tests patch.

The reachability classifier encodes the exact failure this toolkit was born from:
a disco ``tailscale ping`` succeeds but every TCP port is *filtered* — which is an
**ACL gap** (no ``sanctum-host → sanctum-admin`` rule), NOT a dead sshd. Naming
that distinction in a pure, unit-tested function is the whole point.

Verdict doctrine mirrors ``net.status``: DOWN dominates → DEGRADED, else any
ATTENTION → ATTENTION, else GREEN. UNKNOWN rows are fail-open (an unread probe is
never proof of an outage), so a single flaky probe can't false-alarm the pane.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sanctum_cli.net.status import RowStatus, StatusReport, StatusRow

# ── probe value objects (produced by the impure seams in commands.tailnet) ───


@dataclass(frozen=True)
class SpineState:
    """Is this node on the tailnet, and what is its MagicDNS suffix?

    ``on_tailnet`` comes from a 100.64.0.0/10 inet on any interface (ip-allow: IANA CGNAT range — Tailscale's fixed address space, a docstring fact, not an endpoint)
    (``heal._spine_from_ifconfig``); ``suffix`` is the per-operator MagicDNS
    suffix (``tailXXXX.ts.net``) or ``""`` when unknown — never a hardcoded one.
    """

    on_tailnet: bool
    suffix: str


@dataclass(frozen=True)
class PeerReach:
    """Reachability of a named peer. ``ping_ok`` is a Tailscale disco ping (the
    overlay is up end-to-end); ``tcp22_open`` is a raw TCP connect to :22 (the ACL
    actually permits the port). Either ``None`` means that leg was not probed."""

    peer: str
    ping_ok: bool | None
    tcp22_open: bool | None


@dataclass(frozen=True)
class CredState:
    """Tailscale API credential health. ``http_code`` is the status of a
    ``GET /api/v2/tailnet/-/acl`` probe: 200 valid, 401/403 rejected, 0 could not
    authenticate/reach, ``None`` not probed. ``source`` labels where the token came
    from (keychain oauth / keychain api-key / env / none)."""

    http_code: int | None
    source: str


@dataclass(frozen=True)
class AclDrift:
    """Does the live tailnet ACL match the local ``acl.hujson``? ``in_sync`` is
    ``None`` when the comparison could not be made (no cred / read failure)."""

    in_sync: bool | None
    summary: str


@dataclass(frozen=True)
class TrifectaState:
    """Which custody legs hold the Tailscale OAuth credential. ``keychain`` is the
    guaranteed tier ``apply``/``doctor`` actually read; ``onepassword`` is ``None``
    when not probed (best-effort); ``providers_row`` is whether a live
    ``sync_mirrors`` row points the daily drift-sync at the cred."""

    keychain: bool
    onepassword: bool | None
    providers_row: bool


# ── pure classifiers (individually unit-tested) ──────────────────────────────


def classify_reachability(ping_ok: bool | None, tcp22_open: bool | None) -> tuple[RowStatus, str]:
    """Map (disco-ping, TCP:22) into a row status + human detail.

    The load-bearing case: ping OK **and** :22 filtered ⇒ the overlay is healthy
    but the policy blocks the port — an ACL gap, not a dead host. That is the exact
    miss this toolkit exists to name, so it gets its own explicit branch + message.
    """
    if tcp22_open:
        return RowStatus.OK, "SSH :22 reachable"
    if ping_ok and tcp22_open is False:
        return (
            RowStatus.DOWN,
            "disco ping OK but TCP :22 filtered — ACL gap "
            "(no sanctum-host → sanctum-admin:22 rule); run: sanctum tailnet apply",
        )
    if ping_ok is False and tcp22_open is False:
        return RowStatus.DOWN, "peer unreachable (no disco ping, :22 filtered)"
    if tcp22_open is False:
        # :22 closed, disco ping not probed — reachable-but-blocked can't be told
        # apart from down, so report the honest observable: the port is closed.
        return RowStatus.DOWN, "SSH :22 not reachable"
    return RowStatus.UNKNOWN, "probe unavailable"


def classify_cred(http_code: int | None) -> tuple[RowStatus, str]:
    """Map a ``GET /acl`` HTTP status into a credential row status + detail."""
    if http_code is None:
        return RowStatus.UNKNOWN, "probe unavailable"
    if http_code == 200:
        return RowStatus.OK, "valid (GET /acl → 200)"
    if http_code in (401, 403):
        return RowStatus.DOWN, f"rejected ({http_code}) — run: sanctum tailnet creds"
    if http_code == 0:
        return RowStatus.DOWN, "no working credential (could not authenticate)"
    return RowStatus.ATTENTION, f"unexpected API status {http_code}"


_COMMENT_RE = re.compile(r"//[^\n]*")


def _normalize_hujson(text: str) -> str:
    """Strip ``//`` line comments, all whitespace, and hujson trailing commas so two
    hujson texts that differ only in comments/formatting compare equal. A heuristic
    (not a parse) — enough for an advisory drift row, and it never raises.

    Trailing commas matter: the local ``acl.hujson`` is hujson (``[],``) but
    Tailscale's ``GET /acl`` returns strict JSON (``[]``), so without this every
    comparison would falsely report drift. After whitespace removal a ``,}`` / ``,]``
    can only be a trailing comma, so collapsing them is safe.
    """
    compact = "".join(_COMMENT_RE.sub("", text).split())
    return compact.replace(",}", "}").replace(",]", "]")


def diff_acl(local_text: str, live_text: str) -> AclDrift:
    """Advisory drift check: is the live ACL the same policy as local ``acl.hujson``?

    Comment- and whitespace-insensitive (the live copy is re-serialized by
    Tailscale, so byte equality never holds). Equal-after-normalize ⇒ in sync.
    """
    if _normalize_hujson(local_text) == _normalize_hujson(live_text):
        return AclDrift(in_sync=True, summary="live ACL matches local acl.hujson")
    return AclDrift(
        in_sync=False,
        summary="live ACL differs from local acl.hujson — run: sanctum tailnet apply",
    )


# ── row mappers + report assembler (mirror net.status) ───────────────────────


def _spine_row(spine: SpineState | None) -> StatusRow:
    if spine is None:
        return StatusRow("Tailnet", RowStatus.UNKNOWN, "probe unavailable")
    if spine.on_tailnet:
        suffix = f" · {spine.suffix}" if spine.suffix else ""
        return StatusRow("Tailnet", RowStatus.OK, f"joined{suffix}")
    return StatusRow("Tailnet", RowStatus.DOWN, "not on the tailnet — run: sudo tailscale up")


def _peer_row(peer: PeerReach | None) -> StatusRow:
    if peer is None:
        return StatusRow("Peer reach", RowStatus.UNKNOWN, "probe unavailable")
    status, detail = classify_reachability(peer.ping_ok, peer.tcp22_open)
    return StatusRow(f"Peer {peer.peer}", status, detail)


def _cred_row(cred: CredState | None) -> StatusRow:
    if cred is None:
        return StatusRow("API credential", RowStatus.UNKNOWN, "probe unavailable")
    status, detail = classify_cred(cred.http_code)
    return StatusRow("API credential", status, f"{detail} [{cred.source}]")


def _drift_row(drift: AclDrift | None) -> StatusRow:
    if drift is None:
        return StatusRow("ACL drift", RowStatus.UNKNOWN, "probe unavailable")
    if drift.in_sync is None:
        return StatusRow("ACL drift", RowStatus.UNKNOWN, drift.summary)
    return StatusRow(
        "ACL drift", RowStatus.OK if drift.in_sync else RowStatus.ATTENTION, drift.summary
    )


def _trifecta_row(trifecta: TrifectaState | None) -> StatusRow:
    if trifecta is None:
        return StatusRow("Trifecta", RowStatus.UNKNOWN, "probe unavailable")
    legs = ["keychain ✓" if trifecta.keychain else "keychain ✗"]
    if trifecta.onepassword is not None:
        legs.append("1P ✓" if trifecta.onepassword else "1P ✗")
    legs.append("providers.yaml ✓" if trifecta.providers_row else "providers.yaml ✗")
    detail = " · ".join(legs)
    # The keychain leg is the guaranteed tier apply/doctor read; its absence is the
    # actionable one (the 1P/SOPS mirror is best-effort and self-heals via sync.py).
    if not trifecta.keychain:
        return StatusRow("Trifecta", RowStatus.ATTENTION, f"{detail} — run: sanctum tailnet creds")
    return StatusRow("Trifecta", RowStatus.OK, detail)


def build_tailnet_report(
    *,
    spine: SpineState | None,
    peer: PeerReach | None,
    cred: CredState | None,
    drift: AclDrift | None,
    trifecta: TrifectaState | None,
) -> StatusReport:
    """PURE: assemble the five subsystem inputs into a roll-up + OVERALL verdict.

    Each ``None`` input marks a failed probe → UNKNOWN row (fail-open; the pane
    never crashes). Verdict: any DOWN → DEGRADED, else any ATTENTION → ATTENTION,
    else GREEN. UNKNOWN never moves the verdict.
    """
    rows = (
        _spine_row(spine),
        _peer_row(peer),
        _cred_row(cred),
        _drift_row(drift),
        _trifecta_row(trifecta),
    )
    statuses = {row.status for row in rows}
    if RowStatus.DOWN in statuses:
        overall = "DEGRADED"
    elif RowStatus.ATTENTION in statuses:
        overall = "ATTENTION"
    else:
        overall = "GREEN"
    return StatusReport(rows=rows, overall=overall)
