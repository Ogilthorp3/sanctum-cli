"""Layer-2 apple-like intents — compose Layer-1 providers with the safety rails.

A Layer-1 :class:`~sanctum_cli.devices.base.DeviceProvider` gives full, direct
control of a device. An *intent* is the apple-like surface on top: it names a
high-level outcome ("put the WAN behind a single NAT"), figures out the
brand-specific change that achieves it, and runs that change through
:func:`~sanctum_cli.devices.rails.guarded_apply` so it is snapshot-protected,
confirmed (unless ``force``), verified against the real world, and rolled back
on failure.

The one intent shipped in Phase 1 is :func:`single_nat`. Flipping a Bell hub
into bridge mode briefly drops the whole household's internet, and the cutover is
**attended-only** — so the intent is *dry-run by default*. ``single_nat(...)``
with the default ``apply=False`` returns a human-readable plan and makes **zero**
``set`` calls; a caller must pass ``apply=True`` to actually fire it. Nothing in
this module mutates a device on import or by default; the overnight build never
fires it against live gear.

The default verification is real-site reachability via
:func:`sanctum_cli.net.verify.verify` (does a known-good site resolve+connect
after the flip?). A caller may inject ``verify_fn`` directly (the tests do) to
exercise the rails without a runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sanctum_cli.devices.base import Capability, CapabilityOp, DeviceError
from sanctum_cli.devices.rails import guarded_apply
from sanctum_cli.net import verify
from sanctum_cli.net.types import Verdict

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sanctum_cli.devices.base import DeviceProvider, OpResult
    from sanctum_cli.net.detect import Runner

# Reference binding for a Bell/Sagemcom hub: the leaf that, set to ``"on"``, puts
# the gateway into bridge mode. The intent NO LONGER hardcodes this — it resolves
# the concrete (path, engaged) from ``provider.capability_op(BRIDGE_MODE)`` so a
# non-TR-069 brand is driven through its own vocabulary. This constant is kept
# only as the documented Sagemcom default + a back-compat export (tests); it is
# not what single_nat mutates.
BRIDGE_MODE_PATH = "Device/Services/BellNetworkCfg/SetBridgeMode"

# Bell's PPPoE/GPON path black-holes >1492-byte DF packets: after bridge mode the
# downstream router's WAN MTU must be 1492 (+ MSS clamp) or HTTPS silently hangs
# while ping still works. Surfaced in every plan so the operator sets it.
MTU_NOTE = "After cutover, set the downstream router's WAN MTU to 1492 (+MSS clamp) — Bell's path MTU."


def _resolve_bridge_op(provider: DeviceProvider) -> CapabilityOp:
    """Resolve the provider's brand-specific bridge-mode op, or fail legibly.

    The single seam through which the brand-agnostic intent reaches a concrete
    path/value. A provider that does not support :attr:`Capability.BRIDGE_MODE`
    (e.g. the read-only generic fallback) returns ``None`` and we raise a clean
    :class:`DeviceError` instead of mutating an unknown leaf.
    """
    op = provider.capability_op(Capability.BRIDGE_MODE)
    if op is None:
        msg = f"{provider.brand} ({provider.kind}) does not support bridge mode (single-NAT)"
        raise DeviceError(
            msg,
            fix="use a hub provider that advertises Capability.BRIDGE_MODE, or pin the right brand.",
        )
    return op


@dataclass(frozen=True)
class IntentResult:
    """The outcome of running (or dry-running) an intent.

    ``plan`` is always populated — a human-readable, ordered description of what
    the intent will (or did) do, suitable for printing before an apply. ``applied``
    is ``False`` for a dry-run (in which case ``result`` is ``None`` and no device
    was mutated) and ``True`` when the change was fired through ``guarded_apply``
    (``result`` then carries that op's :class:`OpResult`, whose ``ok`` reflects the
    verify verdict / rollback).
    """

    plan: list[str]
    applied: bool
    result: OpResult | None = field(default=None)


def _default_confirm(_plan: str) -> bool:
    """Conservative default: refuse unless an explicit confirm is supplied.

    An intent fired with ``apply=True`` but no ``confirm`` and no ``force`` would
    otherwise have no human in the loop; defaulting to *decline* keeps the
    attended-only contract — the caller must pass a real ``confirm`` (the CLI) or
    ``force=True``.
    """
    return False


def _bridge_mode_plan(op: CapabilityOp) -> list[str]:
    """The ordered, human-readable steps the single-NAT intent performs.

    ``op`` is the provider-resolved bridge-mode binding, so the plan reflects the
    actual (brand-specific) path/value rather than a hardcoded Bell XPath.
    """
    return [
        "single-NAT cutover plan:",
        f"  1. set {op.path} = {op.engaged}  (hub → bridge mode)",
        "  2. verify: real-site reachability through the downstream router",
        "  3. on verify failure: roll back to the pre-change snapshot",
        f"  note: {MTU_NOTE}",
    ]


def single_nat(
    provider: DeviceProvider,
    *,
    force: bool,
    apply: bool = False,
    verify_fn: Callable[[], bool] | None = None,
    runner: Runner | None = None,
    confirm: Callable[[str], bool] | None = None,
    rollback: bool = True,
    log_path: Path | None = None,
) -> IntentResult:
    """Put the hub behind a single NAT (bridge mode), guarded + dry-run by default.

    Resolves the brand-specific bridge-mode op via
    ``provider.capability_op(Capability.BRIDGE_MODE)`` and composes that change
    with the :func:`~sanctum_cli.devices.rails.guarded_apply` rails — so the
    intent stays brand-agnostic and never hardcodes a TR-069 XPath. The flip
    briefly drops the household's internet, so:

    * ``apply=False`` (the default) is a **dry-run**: it returns the plan and makes
      **no** ``set`` calls. This is what the overnight build runs.
    * ``apply=True`` fires the change through ``guarded_apply`` — snapshot →
      confirm (unless ``force``) → set → verify → rollback-on-failure — and returns
      the resulting :class:`OpResult` in ``IntentResult.result``.

    Verification: if ``verify_fn`` is given it is used directly; otherwise the
    default is real-site reachability via :func:`sanctum_cli.net.verify.verify`
    over ``runner`` (a single-NAT :class:`~sanctum_cli.net.types.Verdict.VERIFIED`
    is the only passing verdict). A real apply therefore needs *either* a
    ``verify_fn`` or a ``runner``.

    Raises :class:`DeviceError` if the provider does not support bridge mode.
    """
    # Resolve the provider's bridge-mode binding up front so even a dry-run plan
    # reflects the real (brand-specific) path/value — and an unsupported provider
    # fails legibly before any mutation.
    op = _resolve_bridge_op(provider)
    plan = _bridge_mode_plan(op)
    if not apply:
        # Dry-run: describe, do not mutate. The hard guardrail for the overnight
        # build — no provider.set / guarded_apply is reached on this branch.
        return IntentResult(plan=plan, applied=False, result=None)

    def change(pv: DeviceProvider) -> None:
        pv.set(op.path, op.engaged)

    if verify_fn is None:
        if runner is None:
            msg = "single_nat(apply=True) needs a verify_fn or a runner to verify the cutover"
            raise DeviceError(
                msg,
                fix="pass verify_fn=<callable> or runner=<Runner> so the cutover can be verified",
            )
        bound_runner = runner

        def _verify() -> bool:
            verdict, _reason = verify.verify(runner=bound_runner)
            return verdict is Verdict.VERIFIED

        resolved_verify = _verify
    else:
        resolved_verify = verify_fn

    result = guarded_apply(
        provider,
        change,
        verify_fn=resolved_verify,
        confirm=confirm or _default_confirm,
        force=force,
        rollback=rollback,
        log_path=log_path,
    )
    return IntentResult(plan=plan, applied=True, result=result)
