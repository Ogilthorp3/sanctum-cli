"""Layer-2 safety rails: the one seam every mutating device op goes through.

A Layer-1 :class:`~sanctum_cli.devices.base.DeviceProvider` gives full, direct
control of a device — including the ability to brick a household's WAN with one
bad ``set``. :func:`guarded_apply` is the apple-like wrapper that makes those
mutations *safe by construction*: it snapshots first, asks for an explicit
confirmation (unless ``force``), runs the change, **verifies** the result against
the real world, and on a failed verify rolls the device straight back to the
snapshot. Every outcome is appended to a JSONL audit log so a 2 a.m. cutover
leaves a paper trail.

The ordering — snapshot → confirm → apply → verify → rollback — mirrors
``sanctum net``'s existing single-NAT runbook (see :mod:`sanctum_cli.net.safety`
for the topology-baseline snapshot and :mod:`sanctum_cli.net.verify` for the
single-NAT verdict an intent passes in as ``verify_fn``). This module owns the
*device-state* snapshot/rollback (via the provider) and the audit trail; the net
layer owns the *network-fact* baseline. Intents (see
:mod:`sanctum_cli.devices.intents`) compose the two.

Nothing here ever fires on import or by default — a caller must invoke
``guarded_apply`` explicitly, and a mutating intent defaults to dry-run. The
overnight build must never mutate live gear.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from sanctum_cli.devices.base import DeviceProvider, OpResult, Snapshot

# Where the device-mutation audit trail lands by default. Mirrors the rest of the
# fleet's convention (~/.sanctum/logs/<name>-audit.jsonl, append-only, 0600). A
# caller/test may override via the ``log_path`` argument so unit tests never write
# to the real home directory.
DEFAULT_AUDIT_LOG = Path.home() / ".sanctum/logs/netgear-audit.jsonl"


def _plan_text(provider: DeviceProvider) -> str:
    """A short human-readable description of what is about to be mutated."""
    return f"apply change to {provider.brand} ({provider.kind})"


# Surfaced verbatim when a rollback ITSELF fails — the worst-case 2 a.m. path
# (the change that triggered rollback may have killed the route to the hub). The
# operator must know the device is half-applied and how to recover by hand.
ROLLBACK_FAILED_PREFIX = "ROLLBACK FAILED — device left half-applied"
MANUAL_RECOVERY_FIX = (
    "restore the hub manually: open its admin UI (or re-run once the transport "
    "is reachable) and revert the change to the pre-cutover state."
)


def _attempt_rollback(provider: DeviceProvider, snap: Snapshot) -> tuple[bool, str | None]:
    """Try ``provider.rollback(snap)``; never raise.

    Returns ``(restored, error)``: ``restored`` is True only when the provider
    reported a successful restore (``OpResult.ok``); ``error`` is a short reason
    string when the rollback raised or reported failure, else ``None``. Wrapping
    this is essential — a rollback that raises (transport died after the change
    dropped the WAN, a very plausible state) would otherwise propagate raw past
    the audit + return, leaving the most dangerous outcome with no paper trail
    and only a stack trace for the operator.
    """
    try:
        res = provider.rollback(snap)
    except Exception as exc:  # the rollback transport can die mid-recovery
        return False, str(exc)
    if not res.ok:
        return False, res.detail
    return True, None


def _audit(
    log_path: Path,
    *,
    brand: str,
    kind: str,
    ok: bool,
    detail: str,
    rolled_back: bool,
    before: str | None,
    after: str | None,
) -> None:
    """Append one JSON audit line. Best-effort: never let logging mask the result.

    Written with ``O_APPEND`` at mode 0600 to match the fleet's audit-log
    convention (see :mod:`sanctum_cli.telemetry`). The parent directory is created
    if needed. Any I/O failure is swallowed — a missing audit line must not turn a
    successful (or safely-rolled-back) op into an exception.
    """
    record = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "event": "guarded_apply",
        "brand": brand,
        "kind": kind,
        "ok": ok,
        "detail": detail,
        "rolled_back": rolled_back,
        "before": before,
        "after": after,
    }
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        # Audit is a best-effort side channel; a write failure must not change the
        # operational outcome the caller is relying on.
        pass


def _handle_failure(
    provider: DeviceProvider,
    snap: Snapshot,
    log_path: Path,
    *,
    before: str | None,
    reason: str,
    rollback: bool,
) -> OpResult:
    """Build the OpResult + audit line for a failed apply (raised or verify-fail).

    Distinguishes three terminal states so the operator is never misled:

    * ``rollback=False`` → the failed change is left in place for inspection.
    * rollback ran and the device was restored → "rolled back".
    * **rollback was attempted and FAILED** → the device is half-applied. This is
      the worst case (e.g. the household internet is still down) and was the only
      branch with no defensive handling: a raising ``rollback`` used to propagate
      past the audit + return. We now catch it, audit it distinctly
      (``ok=False, rolled_back=False``), and put an explicit manual-recovery
      instruction in ``detail`` so the CLI surfaces it instead of a stack trace.
    """
    from sanctum_cli.devices.base import OpResult

    if not rollback:
        detail = f"{reason}: left in place"
        result = OpResult(ok=False, detail=detail, before=before, after=None)
        _audit(
            log_path,
            brand=provider.brand,
            kind=provider.kind,
            ok=False,
            detail=detail,
            rolled_back=False,
            before=before,
            after=None,
        )
        return result

    restored, rb_error = _attempt_rollback(provider, snap)
    if restored:
        detail = f"{reason}: rolled back"
    else:
        # The dangerous half-applied state: name it loudly + how to recover.
        detail = f"{reason}: {ROLLBACK_FAILED_PREFIX} ({rb_error}). {MANUAL_RECOVERY_FIX}"
    result = OpResult(ok=False, detail=detail, before=before, after=None)
    _audit(
        log_path,
        brand=provider.brand,
        kind=provider.kind,
        ok=False,
        detail=detail,
        rolled_back=restored,
        before=before,
        after=None,
    )
    return result


def guarded_apply(
    provider: DeviceProvider,
    change: Callable[[DeviceProvider], OpResult | None],
    verify_fn: Callable[[], bool],
    *,
    confirm: Callable[[str], bool],
    force: bool,
    rollback: bool,
    log_path: Path | None = None,
) -> OpResult:
    """Run ``change`` on ``provider`` behind snapshot → confirm → verify → rollback.

    The rails, in order:

    1. **Snapshot.** Capture the device's restorable state *before* anything moves,
       so a rollback target always exists even if ``change`` partially applied.
    2. **Confirm.** Unless ``force`` is set, call ``confirm(plan)``; a falsey return
       aborts with ``ok=False`` and *no* mutation. ``force=True`` skips this gate
       entirely (the ``confirm`` callable is never invoked).
    3. **Apply.** Run ``change(provider)``. Two ways the change can fail:

       * it **raises** mid-flight — the worst case (a half-applied device); or
       * it **returns an** :class:`~sanctum_cli.devices.base.OpResult` **with**
         ``ok=False`` — a *return-convention* provider (FirewallaProvider.set,
         OrbiProvider.set on an unwritable leaf) signals a refused write by
         returning ``ok=False`` rather than raising. The rails INSPECT what the
         closure returns: an ``ok=False`` is treated identically to a raise (roll
         back when ``rollback``, report ``ok=False``), so NO call site can silently
         discard an ``ok=False`` by returning it through (the P2 low finding). A
         returned ``None`` (the closure performed its own check, or the provider
         raises on failure) or an ``ok=True`` OpResult falls through to verify.
    4. **Verify.** Call ``verify_fn()``. ``True`` commits (state kept, ``ok=True``).
    5. **Rollback.** On a falsey verify (or a raised / ``ok=False``-returning change)
       *and* ``rollback=True``, call ``provider.rollback(snapshot)`` to restore the
       captured state and report ``ok=False``. With ``rollback=False`` the (failed)
       change is left in place so an operator can inspect it — still ``ok=False``.

    Every terminal outcome appends one line to the audit log (``log_path`` or
    :data:`DEFAULT_AUDIT_LOG`). Returns an :class:`~sanctum_cli.devices.base.OpResult`
    whose ``ok`` reflects the verify result (or the confirm/abort), ``detail`` names
    the outcome, and ``before``/``after`` carry the snapshot brand + post-state for
    the trail.
    """
    from sanctum_cli.devices.base import OpResult

    path = log_path or DEFAULT_AUDIT_LOG
    snap = provider.snapshot()
    before = snap.brand

    # Gate 2: confirmation (skipped under force).
    if not force and not confirm(_plan_text(provider)):
        result = OpResult(
            ok=False,
            detail="aborted: confirmation declined",
            before=before,
            after=None,
        )
        _audit(
            path,
            brand=provider.brand,
            kind=provider.kind,
            ok=False,
            detail=result.detail,
            rolled_back=False,
            before=before,
            after=None,
        )
        return result

    # Gate 3: apply. A raised change is the worst case — restore if we can.
    try:
        outcome = change(provider)
    except Exception as exc:  # any failure mid-change must trip rollback
        return _handle_failure(
            provider,
            snap,
            path,
            before=before,
            reason=f"change raised ({exc})",
            rollback=rollback,
        )

    # A return-convention provider signals a refused write by RETURNING ok=False
    # (it never raised). guarded_apply can only act on what it can SEE, so it
    # inspects the returned OpResult: an ok=False is a failed apply (treated like a
    # raise) and must NOT be allowed to reach the verify gate and commit. This
    # closes the P2 low finding — a closure that returns the OpResult straight
    # through can no longer silently discard an ok=False.
    if outcome is not None and not outcome.ok:
        return _handle_failure(
            provider,
            snap,
            path,
            before=before,
            reason=f"change reported ok=False ({outcome.detail})",
            rollback=rollback,
        )

    # Gates 4 + 5: verify, then commit or roll back.
    verified = verify_fn()
    if verified:
        detail = "verified: change committed"
        result = OpResult(ok=True, detail=detail, before=before, after="committed")
        _audit(
            path,
            brand=provider.brand,
            kind=provider.kind,
            ok=True,
            detail=detail,
            rolled_back=False,
            before=before,
            after="committed",
        )
        return result

    return _handle_failure(
        provider,
        snap,
        path,
        before=before,
        reason="verify failed",
        rollback=rollback,
    )
