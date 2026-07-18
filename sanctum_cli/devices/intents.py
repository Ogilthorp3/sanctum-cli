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

import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sanctum_cli import config
from sanctum_cli.devices import flip
from sanctum_cli.devices.armor import SinglenatArmorInstaller
from sanctum_cli.devices.base import Capability, CapabilityOp, DeviceError, OpResult, Snapshot
from sanctum_cli.devices.rails import guarded_apply
from sanctum_cli.net import verify
from sanctum_cli.net.types import Verdict

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from sanctum_cli.devices.base import DeviceProvider
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


# ─── single_nat_dmz: the staged Advanced-DMZ cutover orchestrator ────────────
#
# ``single_nat`` flips a single bridge-mode leaf. ``single_nat_dmz`` is the FULL
# Bell **Advanced DMZ + /32** cutover: a multi-stage attended operation that puts
# the hub's WAN into DHCP, engages Advanced DMZ, reboots the hub, observes the new
# downstream lease, installs the self-healing armor kit, verifies reachability,
# and arms the watchdog. The *brain* of that sequence is the pure
# :mod:`sanctum_cli.devices.flip` machine (no I/O); this orchestrator is the thin
# I/O driver that consults it and fires each stage through the real seams, the
# whole thing composed behind :func:`~sanctum_cli.devices.rails.guarded_apply` so
# a failed stage unwinds the flip (disable DMZ → re-lease DHCP) and reports
# ``ok=False``. Dry-run by default — ``apply=False`` makes ZERO device writes.


@runtime_checkable
class RebootingProvider(Protocol):
    """A :class:`DeviceProvider` that also exposes a hub ``reboot()``.

    The Advanced-DMZ cutover must reboot the hub for the new WAN/DMZ config to
    take effect (stage ``hub_reboot``). The base ``DeviceProvider`` Protocol does
    not mandate ``reboot``, so the orchestrator narrows to this structural
    sub-Protocol: a provider passed to :func:`single_nat_dmz` must advertise
    :attr:`Capability.REBOOT` and implement ``reboot()`` returning an
    :class:`OpResult` (the Sagemcom hub does — fail-closed on a rejected reboot).
    """

    def reboot(self) -> OpResult:
        """Reboot the device, returning an :class:`OpResult` (raises on rejection)."""
        ...


@runtime_checkable
class ArmorInstaller(Protocol):
    """The single-NAT armor kit installer seam (stages ``stage_armor`` + ``apply_armor``).

    The armor kit (``sanctum-singlenat-armor``: the self-healing ``/32`` + MTU
    DHCP hook, the watchdog, the OTA sentinel) is installed onto the Firewalla +
    the Mini as part of the cutover. The orchestrator drives it through TWO methods
    so the install is mockable in tests and never touches real hosts in the
    overnight build:

    * :meth:`stage` — the PRE-DMZ stage (FIX-2): deploy the ``/32`` hook + MTU clamp
      onto the box and verify it is STRUCTURALLY ARMED (hook present + wired) WHILE
      the LAN is still healthy, so the supersede is in place the instant the
      post-DMZ poison ``/1`` lease arrives. Run before ``enable_dmz``.
    * :meth:`install` — the POST-cutover stage (``apply_armor``): confirm the armor
      came up HEALTHY now that single-NAT is actually live.

    Each returns an :class:`OpResult`: an ``ok=False`` (refused/failed) is treated
    by the rails exactly like any other failed stage — the whole flip unwinds.
    """

    def stage(self) -> OpResult:
        """Deploy + structurally arm the kit pre-DMZ, returning an :class:`OpResult`."""
        ...

    def install(self) -> OpResult:
        """Confirm the armor is HEALTHY post-cutover, returning an :class:`OpResult`."""
        ...


# The Firewalla-runner tags the DMZ orchestrator fires, in the net layer's
# ``Runner`` vocabulary (``Callable[[tuple[str, ...]], str]``). Named constants so
# the driver and the (mocked) runner never drift on a string literal:
#  * WAN→DHCP/PPPoE passthrough so the downstream router can pull the public lease;
#  * observe the new downstream WAN lease (returns the address to classify);
#  * arm the watchdog/sentinel after the armor lands;
#  * the rollback re-lease (disable DMZ then re-pull a DHCP lease downstream).
_RUNNER_WAN_DHCP = ("wan_dhcp",)
_RUNNER_LEASE_OBSERVE = ("lease_observe",)
_RUNNER_ARMOR_ARM = ("armor_arm",)
_RUNNER_DHCP_RELEASE = ("dhcp_release",)
# FIX (c): raw readbacks that KEEP the /PREFIX + the route table (which the
# IPv4-only ``lease_observe`` strips), so the poison gate can prove the /32 armor
# is holding. Resolved by the net-layer runner's raw-readback tags.
_RUNNER_WAN_ADDR_CIDR = ("wan_addr_cidr",)
_RUNNER_WAN_ROUTES = ("wan_routes",)

# FIX (a): the bounded settle-poll window for ``observe_lease``. After the hub
# reboot the downstream WAN is normally dark (apipa/none) for the 2-5 min hub-reboot
# window before the public lease arrives; the poll waits THROUGH that window before
# declaring failure. 360s = 6 min (> the worst-case 5-min window + ~1 min margin);
# a 15s poll cadence. Exposed as constants so the window is easy to retune and so
# tests can drive a tiny real timeout against the real loop.
_SETTLE_TIMEOUT_S = 360.0
_SETTLE_POLL_INTERVAL_S = 15.0

# FIX (a-2): the bounded dark-window RIDE for the ACTIVE post-reboot box ops — the
# ``wan_dhcp`` re-lease (a flip stage) and the rollback's ``dhcp_release``. Unlike
# ``observe_lease`` (which settle/polls a LEASE CLASS), these ride the box's
# REACHABILITY: the op reaches the box only over the link the cutover bounces, so during
# the 2-5 min hub-reboot window its SSH transport fails. The ride RETRIES the op THROUGH
# that window, bounded by a monotonic deadline; a single shot false-failed the instant
# the box was unreachable (the 2026-06-27 "ROLLBACK FAILED, half-applied"). 480 s = 8 min
# (> the worst-case 5-min hub reboot + margin — wider than observe_lease's 360 s because
# the rollback re-lease rides a SECOND reboot, the latch-reboot); a 15 s cadence. Exposed
# as constants so the window is tunable and tests can drive a tiny real timeout.
_BOX_OP_TIMEOUT_S = 480.0
_BOX_OP_POLL_INTERVAL_S = 15.0

# Default deploy coordinates for the armor-kit installer when a caller does not
# inject an ``armor=`` seam. They mirror the kit README's deploy section (the
# checkout dir + the Firewalla and the Mini jump host); a caller that needs other
# coordinates passes a pre-built ``armor=`` installer (and tests always do, so the
# overnight build never reaches these against live gear). The kit checkout dir is
# NO LONGER a hardcode (FIX-d2): it resolves config-first via :func:`_armor_kit_dir`
# (``paths.armor_kit_dir`` → this default), so a fresh operator whose checkout lives
# elsewhere points the deploy at their own path. This constant is the shipped
# fallback only.
_DEFAULT_ARMOR_KIT_DIR = "/Users/bert/Documents/Claude_Code/sanctum-singlenat-armor"
# The SHIPPED LAN defaults — the general-purpose tool is unchanged for other users.
# Bert's haus pins the tailnet transport in ~/.sanctum/instance.yaml (FIX-b); these
# defaults are NEVER his personal tailnet IPs, only the LAN coordinates the kit
# README documents.
_DEFAULT_ARMOR_FIREWALLA_HOST = "10.0.0.1"  # ip-allow: shipped LAN default for the armor deploy target (Firewalla); overridden config-first via devices.firewalla.host
_DEFAULT_ARMOR_FIREWALLA_USER = "pi"
_DEFAULT_ARMOR_MINI_HOST = "bert@10.0.0.10"  # ip-allow: shipped LAN default for the armor Mini jump host; overridden config-first via devices.mini.host


def _armor_firewalla_host() -> str:
    """The box (Firewalla) host the armor scp/ssh targets — config-first (FIX-b).

    Reads ``devices.firewalla.host`` from instance.yaml at CALL TIME (so a haus on
    the off-LAN cutover perch pins its tailnet box IP), falling back to the shipped
    LAN default. The key is shared with the SSH runner + recovery re-lease so the
    armor deploy, the box reads, and the unwind all reach the SAME box.
    """
    return str(config.instance_value("devices.firewalla.host", _DEFAULT_ARMOR_FIREWALLA_HOST))


def _armor_firewalla_user() -> str:
    """The box SSH user the armor scp/ssh uses — ``devices.firewalla.ssh_user``, else 'pi'."""
    return str(config.instance_value("devices.firewalla.ssh_user", _DEFAULT_ARMOR_FIREWALLA_USER))


def _armor_mini_host() -> str:
    """The Mini ``user@host`` the armor scp/ssh targets — ``devices.mini.host``, else LAN.

    The tailnet pin (a ``bert@<tailnet-ip>`` host) lets the kit deploy reach the
    Mini over Tailscale when the operator is off the ``10/8`` LAN.
    """
    return str(config.instance_value("devices.mini.host", _DEFAULT_ARMOR_MINI_HOST))


def _armor_kit_dir() -> str:
    """The local armor-kit checkout dir the installer scp's from — config-first (FIX-d2).

    Reads ``paths.armor_kit_dir`` from instance.yaml at CALL TIME, falling back to
    the shipped :data:`_DEFAULT_ARMOR_KIT_DIR` so the general-purpose tool is
    unchanged on a fresh box. This is the ONE resolver shared by both
    :func:`_default_armor_installer` (the un-injected intents fallback) and the CLI's
    ``net._build_armor_installer``, so the two never drift on the checkout path — a
    fresh operator whose ``sanctum-singlenat-armor`` lives outside Bert's OneDrive
    tree points BOTH deploy seams at their own checkout with one config key.
    """
    return str(config.instance_value("paths.armor_kit_dir", _DEFAULT_ARMOR_KIT_DIR))


def _default_armor_installer() -> ArmorInstaller:
    """Build the real :class:`SinglenatArmorInstaller` from config-or-default coordinates.

    The single seam through which an un-injected :func:`single_nat_dmz` reaches a
    concrete armor install. The box + Mini hosts are resolved config-first (FIX-b:
    ``devices.firewalla.host`` / ``devices.firewalla.ssh_user`` / ``devices.mini.host``)
    so an off-LAN operator's deploy rides the tailnet, defaulting to the LAN
    coordinates so the shipped tool is unchanged. Constructed lazily (only on the
    apply path) so the dry-run makes zero host contact, and so tests that swap
    ``intents.SinglenatArmorInstaller`` for a recording double exercise the wiring
    without ever shelling out.
    """
    return SinglenatArmorInstaller(
        kit_dir=_armor_kit_dir(),
        firewalla_host=_armor_firewalla_host(),
        firewalla_user=_armor_firewalla_user(),
        mini_host=_armor_mini_host(),
    )


class _StageError(Exception):
    """A flip stage failed (I/O refused, ``ok=False`` returned, or verify false).

    Raised INSIDE the ``guarded_apply`` change closure so the rails catch it like
    any other raised change and trip rollback — never propagated to the caller
    (``guarded_apply`` converts it to an ``ok=False`` :class:`OpResult`).
    """


def _dmz_plan(op: CapabilityOp, *, requires_slash32_armor: bool = True) -> list[str]:
    """The ordered, human-readable Advanced-DMZ cutover steps (for the dry-run).

    When ``requires_slash32_armor`` is False (FIX-e: a non-Bell ISP whose passthrough
    lease carries no /1 poison) the two ``/32``-armor steps are OMITTED — the cutover
    neither stages nor confirms the armor for an ISP that does not need it — and the
    surviving steps are renumbered so the plan reads honestly. The numbers are derived
    from the actual step list rather than hardcoded, so they never skip a digit.
    """
    steps = [
        "preflight: confirm an out-of-band recovery path exists",
        "put the hub WAN into DHCP/PPPoE passthrough",
    ]
    if requires_slash32_armor:
        steps.append("STAGE the self-healing /32 armor on the box (while the LAN is healthy)")
    steps.append(f"enable Advanced DMZ: set {op.path} = {op.engaged}")
    steps.append("reboot the hub so the new WAN/DMZ config takes effect")
    steps.append("observe the downstream router's new WAN lease + classify it")
    if requires_slash32_armor:
        steps.append("confirm the /32 armor came up HEALTHY now single-NAT is live")
    steps.append("verify real-site reachability through the new single NAT")
    steps.append("arm the watchdog/sentinel so drift self-heals")
    numbered = [f"  {i}. {step}" for i, step in enumerate(steps, start=1)]
    return [
        "single-NAT (Bell Advanced DMZ + /32) cutover plan:",
        *numbered,
        "  on any stage failure: roll back — disable DMZ, then re-lease DHCP",
        f"  note: {MTU_NOTE}",
    ]


def _resolve_dmz_op(provider: DeviceProvider) -> CapabilityOp:
    """Resolve the provider's Advanced-DMZ op, or fail legibly (no blind mutation)."""
    op = provider.capability_op(Capability.DMZ)
    if op is None:
        msg = f"{provider.brand} ({provider.kind}) does not support Advanced DMZ (single-NAT)"
        raise DeviceError(
            msg,
            fix="use a hub provider that advertises Capability.DMZ, or pin the right brand.",
        )
    return op


# Surfaced verbatim when a rollback could not bring the WAN back — the operator
# must know the household is dark and how to recover by hand (mirrors the rails'
# own MANUAL_RECOVERY_FIX wording).
_MANUAL_RECOVERY = (
    "recover by hand: open the hub admin UI, confirm Advanced DMZ is OFF, reboot "
    "the hub, and re-lease the downstream router's WAN until it pulls a lease."
)

# The capabilities a single-NAT cutover can engage — the SAME two leaves the real
# Sagemcom provider lists in ``_MUTATED_XPATHS``: the old ``single_nat`` flipped
# bridge mode, the ``single_nat_dmz`` orchestrator engages Advanced DMZ. A rollback
# that restores the captured pre-cutover baseline must disengage BOTH, not just the
# one the CLI happened to resolve — otherwise a prior bridge-mode flip is left
# silently engaged and the household stays behind a single NAT.
_SINGLE_NAT_CAPABILITIES = (Capability.BRIDGE_MODE, Capability.DMZ)


# Map a brand's ENGAGED sentinel to its DISENGAGED opposite, WITHIN the leaf's own
# value-space. A boolean true/false leaf (Bell's ``AdvancedDMZ/Enable``, engaged
# ``"true"``) MUST disengage to ``"false"`` — NEVER ``"on"``: the SAH boolean leaf
# rejects ``"on"`` with ``XMO_INVALID_PARAMETER_TYPE_ERR``, which left the
# 2026-06-27 ``--rollback`` unable to disable DMZ. An on/off leaf (engaged ``"on"``)
# disengages to ``"off"``. Keyed off the brand's OWN engaged value, so this seam
# learns no brand vocabulary.
_DISENGAGED_SENTINEL: dict[str, str] = {
    "on": "off",
    "off": "on",
    "true": "false",
    "false": "true",
}


def disengaged_value(op: CapabilityOp) -> str:
    """The value that DISENGAGES ``op``'s capability, in the leaf's own value-space.

    The inverse of ``op.engaged`` WITHOUT crossing value-spaces: an on/off leaf →
    ``"off"``; a boolean true/false leaf → ``"false"`` (not ``"on"``, which the SAH
    rejects as a type error). Falls back to ``"off"`` for an unrecognized engaged
    sentinel — the safe "not engaged" default.
    """
    return _DISENGAGED_SENTINEL.get(op.engaged, "off")


def disengaged_baseline_snapshot(provider: DeviceProvider) -> Snapshot:
    """Build the captured pre-cutover baseline: every single-NAT leaf disengaged.

    The honest substitute (FIX-5 c) for the CLI ``--rollback`` path's old
    fabricated single-key ``{dmz_path: disengaged}`` dict. A standalone
    ``--rollback`` has no in-process snapshot from the apply run to restore, so it
    must reconstruct the *pre-cutover* baseline — the state every single-NAT-
    mutating leaf was in before any cutover engaged it: **off**.

    Built through the provider's brand-agnostic :meth:`capability_op` seam for each
    of :data:`_SINGLE_NAT_CAPABILITIES` (bridge mode + Advanced DMZ — the two the
    real provider lists in ``_MUTATED_XPATHS``), so a brand whose engaged value is
    not literally ``"on"`` still gets the correct disengaged value and so adding a
    brand needs no change here. The resulting snapshot disengages BOTH leaves — the
    same coverage the rails' pre-cutover ``provider.snapshot()`` guarantees — rather
    than leaving a prior bridge-mode flip silently engaged.
    """
    data: dict[str, str] = {}
    for capability in _SINGLE_NAT_CAPABILITIES:
        op = provider.capability_op(capability)
        if op is None:
            continue
        # The disengaged value is the brand's OPPOSITE sentinel, inverted within the
        # leaf's own value-space (on/off → "off", boolean true/false → "false") so a
        # boolean leaf is never sent "on" — the SAH type error that broke --rollback.
        data[op.path] = disengaged_value(op)
    return Snapshot(brand=provider.brand, taken_at="pre-cutover-baseline", data=data)


def _verify_recovered_double_nat(runner: Runner) -> tuple[bool, str]:
    """Did the WAN come back to a working (non-APIPA) double-NAT lease post-rollback?

    The rollback's HONEST recovery check (FIX-5 a). After disabling DMZ + rebooting
    + re-leasing, a recovered household is back behind the hub's NAT — double-NAT —
    so the REAL :func:`sanctum_cli.net.verify.verify` over the runner returns
    :attr:`Verdict.NOT_YET` ("still double-NAT"), which here is the *success* state.
    Anything else is a failed recovery:

    * :attr:`Verdict.APIPA_ROLLBACK` — the WAN is self-assigned APIPA: the re-lease
      did not bring it back at all (the household is dark).
    * :attr:`Verdict.VERIFIED` — still a single-NAT public WAN: the DMZ-disable did
      not actually take (the hub is still passing the public lease through).
    * :attr:`Verdict.INCONCLUSIVE` — no lease / could not confirm.

    Returns ``(recovered, reason)``; the verdict is derived from the consumer's
    real ``verify`` contract, never a ``lambda: True`` (honest-verify).
    """
    verdict, reason = verify.verify(runner=runner)
    return verdict is Verdict.NOT_YET, reason


class _DmzRollbackProvider:
    """Wraps the hub so ``guarded_apply``'s rollback re-leases + verifies recovery.

    The rails own the rollback contract (snapshot → on-failure ``rollback(snap)``),
    but the Advanced-DMZ unwind is two steps, not one: *disable DMZ* (the hub
    provider's own ``rollback``, which restores the snapshotted DMZ leaf to "off")
    AND *re-lease DHCP* downstream (so the Firewalla pulls a fresh double-NAT lease
    once the hub is no longer in DMZ — otherwise it sits on the stale single-NAT
    address and the household stays dark). Wrapping the provider keeps that unwind
    inside the rails (one audited rollback path) instead of bolting a second
    recovery call outside ``guarded_apply`` where a failure would go untracked.

    Every other attribute/method is delegated unchanged, so to the rails this is
    the real provider in all respects except the augmented ``rollback``.
    """

    def __init__(self, inner: DeviceProvider, runner: Runner) -> None:
        self._inner = inner
        self._runner = runner

    def __getattr__(self, name: str) -> object:
        # Delegate brand/kind/get/set/reboot/snapshot/capabilities/... unchanged.
        return getattr(self._inner, name)

    def rollback(self, snap: Snapshot) -> OpResult:
        """Disable DMZ → reboot the hub → re-lease DHCP → verify the WAN recovered.

        The Advanced-DMZ unwind is NOT a disable + a blind re-lease. Two things the
        old path got wrong (FIX-5 a + b) and that left the household dark while
        reporting green:

        * **Engaging Advanced DMZ needs a hub reboot to latch — so does disabling
          it.** After the inner rollback restores the DMZ leaf to ``"off"`` we
          ``reboot()`` the hub BEFORE the downstream re-lease, so the disable has
          actually taken effect when the router tries to pull its recovered lease.
          A re-lease against a hub that is still latched in DMZ would just re-pull
          the single-NAT (or APIPA) address.
        * **A swallowed re-lease that did not bring the WAN back is NOT a
          successful rollback.** After the re-lease we re-read + classify the
          downstream WAN via the REAL :func:`sanctum_cli.net.verify.verify` over the
          runner: a recovered network is double-NAT (the hub is NATing again →
          :attr:`Verdict.NOT_YET`); an APIPA / no-lease WAN
          (:attr:`Verdict.APIPA_ROLLBACK` / inconclusive) means recovery FAILED and
          we report ``ok=False`` so the rails surface manual recovery. Reporting a
          swallowed re-lease as green hid a dark network from the operator.

        The inner DMZ-disable's ``ok`` is the hard gate: a failed disable surfaces
        immediately (and we do NOT reboot or re-lease on top of a still-engaged
        DMZ — there is nothing safe to recover *to* while the hub is in Advanced
        DMZ). Only on a successful disable do we reboot → re-lease → verify.
        """
        result = self._inner.rollback(snap)
        if not result.ok:
            # The dangerous leaf is still engaged — surface the failed disable as-is
            # and do NOT reboot/re-lease on top of an un-disabled DMZ.
            return result
        # DMZ is back off. Reboot so the disable LATCHES before the downstream
        # re-lease (engaging DMZ needed a reboot; disabling it needs one too).
        reboot = getattr(self._inner, "reboot", None)
        if callable(reboot):
            rb = reboot()
            if isinstance(rb, OpResult) and not rb.ok:
                return OpResult(
                    ok=False,
                    detail=(
                        f"rollback INCOMPLETE: DMZ disabled but the hub rejected the "
                        f"reboot needed to latch it ({rb.detail}). {_MANUAL_RECOVERY}"
                    ),
                )
        # Now re-pull a downstream lease so the WAN recovers behind the hub's NAT —
        # RIDING the post-reboot dark window (FIX a-2). The hub was JUST rebooted above
        # to latch the disable, so the box's WAN→Tailscale is down for the 2-5 min
        # window; a single SSH shot false-failed the instant the box was unreachable
        # (the 2026-06-27 "ROLLBACK FAILED, half-applied"). The ride retries the re-lease
        # THROUGH the window, failing closed (raising) only if the box never returns by
        # the bound — which we surface as an HONEST ok=False with the manual-recovery
        # instruction (the rollback contract returns an OpResult, it never raises).
        try:
            _ride_dark_window(lambda: self._runner(_RUNNER_DHCP_RELEASE), op="dhcp_release")
        except _StageError as exc:
            return OpResult(
                ok=False,
                detail=(
                    f"rollback INCOMPLETE: DMZ disabled + hub rebooted, but the re-lease "
                    f"never reached the box through the hub-reboot window ({exc}). "
                    f"{_MANUAL_RECOVERY}"
                ),
            )
        # HONEST recovery check: re-read + classify the downstream WAN. A swallowed
        # re-lease that left the WAN APIPA/none did NOT recover the household.
        recovered, why = _verify_recovered_double_nat(self._runner)
        if not recovered:
            return OpResult(
                ok=False,
                detail=(
                    f"rollback INCOMPLETE: DMZ disabled + hub rebooted, but the WAN "
                    f"did not return to a working (non-APIPA) lease — {why}. "
                    f"{_MANUAL_RECOVERY}"
                ),
            )
        return OpResult(
            ok=True,
            detail=f"rolled back: DMZ disabled, hub rebooted, WAN recovered ({why})",
        )


def _settle_max_iters(timeout_s: float, poll_interval_s: float) -> int:
    """A DEFENSIVE iteration cap for the settle poll (the clock is the real bound).

    The poll terminates when the monotonic clock crosses ``timeout_s`` (the pure
    :func:`flip.settle_poll_decision` returns ``hard_fail`` then). This cap only
    guards against a pathological clock that never advances — it mirrors the
    ``len(FLIP_STAGES)+1`` defensive bound the stage walk uses. With a sane clock it
    is never reached. A zero/negative interval (tests drive an instant poll) is
    clock-bounded only, so the cap is generous.
    """
    if poll_interval_s <= 0:
        return 1_000_000
    return int(timeout_s / poll_interval_s) + 2


def _ride_dark_window(
    op_fn: Callable[[], object],
    *,
    op: str,
    timeout_s: float | None = None,
    poll_interval_s: float | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Fire an ACTIVE box op, RIDING the hub-reboot dark window; raise only if it never lands.

    The two active post-reboot box ops — the ``wan_dhcp`` re-lease and the rollback's
    ``dhcp_release`` — reach the box ONLY over the link the cutover is bouncing, so a
    single SSH attempt false-fails the instant the box is unreachable mid-reboot (the
    2-5 min window — the 2026-06-27 "ROLLBACK FAILED, half-applied"). This rides that
    window with the SAME bounded-poll machinery as :func:`_observe_lease` — an injectable
    monotonic clock + sleep, the :func:`_settle_max_iters` defensive cap — driven by the
    pure :func:`flip.box_op_retry_decision`:

    * fire ``op_fn``; if it returns (the box is reachable + the op landed) → done;
    * if it raises :class:`RuntimeError` (the box's SSH transport failed — unreachable
      mid-reboot, the fail-closed runner's signal), consult the decision with the elapsed
      monotonic time:
        - ``retry`` (still inside the window) → ``sleep`` to the next tick and re-fire;
        - ``give_up`` (past the bound — the box never returned) → raise :class:`_StageError`
          so the caller fails closed (the rails unwind for a stage; the rollback surfaces
          manual recovery). Never hangs forever, never masks: a genuinely dead box surfaces
          at the bound, exactly when ``box_op_retry_decision`` says the window is over.

    Only a ``RuntimeError`` (the runner's transport-failure contract) is ridden; any other
    exception is a genuine bug and propagates. ``timeout_s``/``poll_interval_s`` default to
    the module constants read at call time (tunable/monkeypatchable); the real CLI path
    passes neither and rides the full 480 s window. The monotonic clock (never wall-clock)
    means an NTP step while the hub reboots cannot corrupt the elapsed measurement.
    """
    resolved_timeout = _BOX_OP_TIMEOUT_S if timeout_s is None else timeout_s
    resolved_interval = _BOX_OP_POLL_INTERVAL_S if poll_interval_s is None else poll_interval_s
    start = now()
    last_exc: RuntimeError | None = None
    for _ in range(_settle_max_iters(resolved_timeout, resolved_interval)):
        try:
            op_fn()
        except RuntimeError as exc:
            # The box's SSH transport failed — unreachable mid-reboot. Decide retry vs
            # give-up purely on the elapsed monotonic time (never mask: a box that never
            # returns hard-fails at the bound).
            last_exc = exc
            decision = flip.box_op_retry_decision(
                op=op, elapsed_s=now() - start, timeout_s=resolved_timeout
            )
            if decision.action == "give_up":
                msg = f"{decision.reason} (last transport error: {exc})"
                raise _StageError(msg) from exc
            sleep(resolved_interval)
            continue
        return
    # Unreachable with a sane (advancing) clock — give_up fires at the bound first.
    # Fail-closed if the clock somehow never advanced.
    msg = f"box op {op!r}: dark-window ride exhausted its iteration cap"
    raise _StageError(msg) from last_exc


def _assert_wan_not_poisoned(runner: Runner, *, requires_slash32_armor: bool = True) -> None:
    """FIX (c): refuse to commit a "public" lease still carrying Bell's /1 poison.

    A ``public`` lease can still be the 2026-06-26 condition — a public IP with a
    ``/1`` netmask + a ``0.0.0.0/1`` on-link route that collapses LAN forwarding —
    if the ``/32`` armor's supersede did not hold. ``lease_observe`` strips the
    prefix, so we read the WAN's raw CIDR + route table (the runner's raw-readback
    tags) and consult the pure :func:`flip.evaluate_wan_poison`.

    ``requires_slash32_armor`` (FIX-e) is the per-playbook gate: True for the Bell
    Advanced-DMZ cutover (committable IFF the WAN is pinned to ``/32`` AND no
    ``0.0.0.0/1`` route is present); False for every other ISP (a healthy public lease
    of any prefix commits — there is no /1 poison to supersede). A non-committable
    verdict raises :class:`_StageError` so ``guarded_apply`` unwinds — a poisoned
    public lease can never commit green. The raw reads fail-CLOSED: if they raise
    (LAN-SSH down at the commit moment) the raise propagates and the rails roll back,
    because we cannot PROVE the armor holds.
    """
    verdict = flip.evaluate_wan_poison(
        runner(_RUNNER_WAN_ADDR_CIDR),
        runner(_RUNNER_WAN_ROUTES),
        requires_slash32_armor=requires_slash32_armor,
    )
    if not verdict.committable:
        msg = f"observe_lease: {verdict.reason}"
        raise _StageError(msg)


def _observe_lease(
    runner: Runner,
    *,
    requires_slash32_armor: bool = True,
    timeout_s: float | None = None,
    poll_interval_s: float | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Bounded settle-poll of the downstream WAN lease; raise on a genuine failure.

    The ``observe_lease`` stage's real I/O. Engaging Advanced DMZ + rebooting the
    hub leaves the downstream WAN dark (apipa/none) for the NORMAL 2-5 min hub-reboot
    window before the public lease arrives — the old single-shot read raced that
    window and FALSE-FAILED a cutover that would have succeeded (FIX a). This polls
    THROUGH the window, driven by the pure :func:`flip.settle_poll_decision`, with an
    injectable monotonic clock + sleep so the suite runs offline and instant:

    * read + classify the lease (:func:`flip.classify_wan_ip`); a transient LAN-SSH
      blip during the reboot (``RuntimeError``) reads as ``"none"`` — *still
      settling*, not an instant fail (the recovery-over-LAN seam can blip mid-reboot);
    * consult the pure decision with the elapsed monotonic time:
        - ``settled_ok`` (a ``public`` lease) → run the poison gate
          (:func:`_assert_wan_not_poisoned`, FIX c) and return (commit);
        - ``hard_fail`` (``double_nat`` at once, or a transient that survived the
          whole window) → raise :class:`_StageError` so the rails unwind (disable
          DMZ + re-lease) rather than leave the hub in DMZ on a dead WAN;
        - ``keep_polling`` → nudge ONE ``dhcp_release`` (a nudge that itself fails
          mid-window is just another settling signal) and ``sleep`` to the next tick.

    ``timeout_s``/``poll_interval_s`` default to the module constants
    (:data:`_SETTLE_TIMEOUT_S`/:data:`_SETTLE_POLL_INTERVAL_S`) — read at call time so
    they remain tunable/monkeypatchable; the real CLI path passes neither. The
    monotonic clock (never wall-clock) means an NTP step while the hub reboots cannot
    corrupt the elapsed measurement. The iteration cap (:func:`_settle_max_iters`) is
    a defensive backstop against a frozen clock; the clock is the real bound.
    """
    resolved_timeout = _SETTLE_TIMEOUT_S if timeout_s is None else timeout_s
    resolved_interval = _SETTLE_POLL_INTERVAL_S if poll_interval_s is None else poll_interval_s
    start = now()
    for _ in range(_settle_max_iters(resolved_timeout, resolved_interval)):
        try:
            observed = flip.classify_wan_ip(runner(_RUNNER_LEASE_OBSERVE))
        except RuntimeError:
            # A transient LAN-SSH blip during the reboot reads as 'still settling',
            # NOT an instant fail — bounded by the timeout, so a genuinely LAN-dark
            # household still (correctly) hard-fails at the bound.
            observed = "none"
        decision = flip.settle_poll_decision(
            observed, elapsed_s=now() - start, timeout_s=resolved_timeout
        )
        if decision.action == "settled_ok":
            # The lease is public — but a Bell public lease can still carry the /1
            # poison if the /32 armor did not hold. Refuse to commit until proven
            # (FIX-e: the /32 requirement is gated per-playbook; a non-Bell public
            # lease of any prefix commits, the Bell /1 guards still apply).
            _assert_wan_not_poisoned(runner, requires_slash32_armor=requires_slash32_armor)
            return
        if decision.action == "hard_fail":
            msg = f"observe_lease: {decision.reason} (observed={observed!r})"
            raise _StageError(msg)
        if decision.re_lease:
            # The nudge failing during the dark window is itself a settling signal —
            # keep polling; the timeout is still the hard bound.
            with contextlib.suppress(RuntimeError):
                runner(_RUNNER_DHCP_RELEASE)
        sleep(resolved_interval)
    # Unreachable with a sane (advancing) clock — the decision hard-fails at the
    # bound first. Fail-closed if the clock somehow never advanced.
    msg = "observe_lease: settle poll exhausted its iteration cap"
    raise _StageError(msg)


def _run_stage(
    stage: str,
    *,
    provider: DeviceProvider,
    op: CapabilityOp,
    runner: Runner,
    armor: ArmorInstaller,
    verifier: Callable[[], bool],
    requires_slash32_armor: bool = True,
) -> None:
    """Fire one flip stage's real I/O, then its verify probe; raise on any failure.

    The single seam between the pure :func:`flip.next_stage` decision and the real
    world. Each stage performs its brand/transport-specific action through a mock-
    able seam (the provider's ``set``/``reboot``, the Firewalla ``runner``, the
    ``armor`` installer) and then consults ``verifier`` — a per-stage probe the
    caller injects (tests pass a mapping; the CLI passes real reachability/lease
    probes). A stage whose I/O raises, RETURNS an ``ok=False`` :class:`OpResult`,
    or whose verify probe returns falsey raises :class:`_StageError` so the
    enclosing ``guarded_apply`` change closure trips the rollback rails.
    """
    if stage == "wan_dhcp":
        # FIX (a-2): the active WAN re-lease RIDES the hub-reboot dark window — a single
        # SSH shot false-failed the instant the box was unreachable mid-reboot. Retries
        # through the window, failing closed only if the box never returns by the bound.
        _ride_dark_window(lambda: runner(_RUNNER_WAN_DHCP), op="wan_dhcp")
    elif stage == "stage_armor":
        # FIX-2: deploy + structurally arm the /32 armor on the box BEFORE DMZ
        # engages, while the LAN is still healthy, so the supersede is in place when
        # the post-reboot poison /1 lease arrives. A failed stage unwinds the flip
        # before the hub is ever touched (worst case: a clean, still-double-NAT box).
        _require_ok(armor.stage(), stage)
    elif stage == "enable_dmz":
        _require_ok(provider.set(op.path, op.engaged), stage)
    elif stage == "hub_reboot":
        _require_ok(_reboot(provider), stage)
    elif stage == "observe_lease":
        _observe_lease(runner, requires_slash32_armor=requires_slash32_armor)
    elif stage == "apply_armor":
        # Post-cutover: confirm the armor (deployed at stage_armor) came up HEALTHY
        # now single-NAT is actually live.
        _require_ok(armor.install(), stage)
    elif stage == "arm":
        runner(_RUNNER_ARMOR_ARM)
    # "preflight" and "verify" carry no mutating I/O of their own — they are pure
    # gate/probe stages whose only effect is the verifier below.

    if not verifier():
        msg = f"stage {stage!r} failed verification"
        raise _StageError(msg)


def _reboot(provider: DeviceProvider) -> OpResult:
    """Reboot the hub via the :class:`RebootingProvider` seam, or fail legibly.

    Duck-typed via ``getattr`` (callable ``reboot``) rather than an ``isinstance``
    against :class:`RebootingProvider`: the rails hand the change closure a
    delegating wrapper (:class:`_DmzRollbackProvider`) whose ``reboot`` is reached
    through ``__getattr__``, and a ``@runtime_checkable`` Protocol ``isinstance``
    check inspects the *class* MRO — so it would not see a method exposed only via
    ``__getattr__`` and would wrongly reject the wrapper. ``getattr`` resolution
    follows the delegation, so it works for both the bare provider and the wrapper.
    """
    reboot = getattr(provider, "reboot", None)
    if not callable(reboot):
        msg = f"{provider.brand} ({provider.kind}) cannot reboot (no reboot())"
        raise _StageError(msg)
    result = reboot()
    if not isinstance(result, OpResult):
        msg = f"{provider.brand} ({provider.kind}).reboot() did not return an OpResult"
        raise _StageError(msg)
    return result


def _require_ok(result: OpResult | None, stage: str) -> None:
    """Treat a returned ``ok=False`` OpResult as a stage failure (return-convention).

    A return-convention provider/installer signals a refused op by RETURNING
    ``ok=False`` (not raising). The orchestrator inspects it and raises
    :class:`_StageError` so the rails roll back — mirroring ``guarded_apply``'s
    own ok=False handling, but at the per-stage granularity inside the walk.
    """
    if result is not None and not result.ok:
        msg = f"stage {stage!r} reported ok=False ({result.detail})"
        raise _StageError(msg)


def _assert_interlock(
    provider: DeviceProvider,
    done: list[str],
    *,
    oob_probe: Callable[[], bool] | None,
    out_of_band_reachable: bool,
    requires_slash32_armor: bool = True,
) -> None:
    """Fail-closed prevent-interlock, evaluated AT the DMZ-engage moment (FIX-3).

    Consults the pure :func:`flip.evaluate_interlock` with the three preconditions
    sampled RIGHT NOW, immediately before the irreversible ``set(DMZ, engaged)``:

    * **OOB channel proven-live** — ``oob_probe()`` if injected (the live,
      moment-of-op Tailscale-on-box re-check, the LAN-independent channel that
      survives a LAN collapse), else the cheap top-of-flight ``out_of_band_reachable``
      boolean. A channel can die between preflight and engage, so the live re-probe
      is the authoritative signal.
    * **armor staged** — ``"stage_armor" in done``: the pre-DMZ armor stage actually
      completed in THIS walk (FIX-2 puts it before ``enable_dmz``), so the ``/32``
      supersede is on the box before the poison ``/1`` can land. When
      ``requires_slash32_armor`` is False (FIX-e: a non-Bell ISP whose lease carries
      no /1 poison) the armor stages are skipped, so this precondition is satisfied
      by construction — there is no poison ``/1`` to supersede.
    * **rollback staged** — a non-empty disengaged baseline exists to unwind to.

    Raises :class:`_StageError` (which ``guarded_apply`` catches → rollback) if any
    precondition is absent, so a refused interlock fires ZERO DMZ writes and the WAN
    is never dropped. ``--force`` does not reach here — it only waives the human
    confirm in ``guarded_apply``; the interlock is never waived.
    """
    oob_live = oob_probe() if oob_probe is not None else out_of_band_reachable
    armor_staged = (not requires_slash32_armor) or ("stage_armor" in done)
    decision = flip.evaluate_interlock(
        oob_channel_live=oob_live,
        armor_staged=armor_staged,
        rollback_staged=bool(disengaged_baseline_snapshot(provider).data),
    )
    if not decision.engage:
        raise _StageError(decision.reason)


def _default_stage_verifier() -> bool:
    """Default per-stage probe when the caller injects none: conservatively pass.

    A stage with no injected verifier has no real-world probe to consult here —
    its I/O either raised or returned ``ok=False`` (already handled) or succeeded.
    The CLI wires real reachability/lease probes for the stages that have one; an
    un-probed stage falls through to the next stage's action, and the terminal
    ``verify`` stage is where the real end-to-end reachability check lives.
    """
    return True


def single_nat_dmz(
    provider: DeviceProvider,
    runner: Runner,
    armor: ArmorInstaller | None = None,
    *,
    apply: bool = False,
    out_of_band_reachable: bool = True,
    oob_probe: Callable[[], bool] | None = None,
    stage_verifiers: Mapping[str, Callable[[], bool]] | None = None,
    confirm: Callable[[str], bool] | None = None,
    force: bool = False,
    rollback: bool = True,
    log_path: Path | None = None,
    requires_slash32_armor: bool = True,
) -> IntentResult:
    """Run the full Bell Advanced-DMZ + /32 single-NAT cutover, guarded + dry-run.

    Walks the pure :data:`sanctum_cli.devices.flip.FLIP_STAGES` machine, firing
    each stage's real I/O through its mockable seam — the hub ``provider``
    (``set`` Advanced DMZ engaged via its own :attr:`Capability.DMZ` op +
    ``reboot``), the Firewalla ``runner`` (WAN→DHCP passthrough + observe the
    downstream lease + arm the watchdog), and the ``armor`` installer — and
    composes the whole sequence behind
    :func:`~sanctum_cli.devices.rails.guarded_apply`.

    ``armor`` is the :class:`ArmorInstaller` for the ``apply_armor`` stage. A caller
    (CLI/test) may inject one; when omitted (``None``) the real
    :class:`~sanctum_cli.devices.armor.SinglenatArmorInstaller` is constructed
    lazily on the apply path (never on the dry-run/gate-refused paths, which make
    zero host contact) so a real cutover deploys the kit without every call site
    hand-building the seam.

    * ``apply=False`` (the default) is a **dry-run**: it resolves the DMZ op (so an
      unsupported hub fails legibly) and returns the staged plan, making **ZERO**
      device writes — no ``set``, no ``reboot``, no ``runner`` op, no armor install.
      This is what the overnight build runs.
    * ``out_of_band_reachable=False`` **refuses** the flip with ``ok=False`` and
      makes zero writes: the cutover drops the WAN and a misstep could strand the
      household dark with no recovery path (:func:`flip.gate_ok`). Checked before
      any mutation.
    * ``oob_probe`` is the LIVE, moment-of-op recovery re-check the prevent-interlock
      (FIX-3) consults AT the ``enable_dmz`` seam — the authoritative gate. When
      injected (the CLI passes the Tailscale-first ``_out_of_band_reachable``) it is
      re-sampled immediately before the irreversible DMZ ``set``, so a channel that
      died between preflight and engage refuses the engage with ZERO DMZ writes. When
      ``None`` the interlock falls back to the ``out_of_band_reachable`` boolean
      already proven at the top. The interlock ALSO requires the ``/32`` armor staged
      (``stage_armor`` completed before ``enable_dmz``, FIX-2) and a rollback baseline
      captured — fail-closed unless all three hold. ``force`` never waives it.
    * ``apply=True`` (with the gate passed) drives the stages through
      ``guarded_apply``: snapshot → confirm (unless ``force``) → walk the stages →
      on the first failed stage (I/O raised, ``ok=False`` returned, or a stage
      verify probe failed) the rails roll back — disable DMZ (``provider.rollback``)
      then re-lease DHCP downstream — and report ``ok=False``.

    ``stage_verifiers`` injects a per-stage probe (``stage name → () -> bool``);
    tests pass a mapping to drive each branch deterministically, and the CLI wires
    real reachability/lease probes. A stage with no entry uses
    :func:`_default_stage_verifier`.

    ``requires_slash32_armor`` (FIX-e) decouples the ``/32`` armor from the cutover so
    it is no longer Bell-only-hardcoded. True (the default) is the Bell Advanced-DMZ
    behavior: the ``stage_armor`` + ``apply_armor`` stages run and the poison gate
    requires the WAN pinned to ``/32``. False (resolved by the CLI from the matched
    playbook's ``requires_slash32_armor`` flag, set only for Bell) SKIPS both armor
    stages and accepts a healthy public lease of ANY prefix — for an ISP whose
    passthrough yields a normal public lease with no Bell ``/1`` poison.

    Raises :class:`DeviceError` if the provider does not support Advanced DMZ.
    """
    op = _resolve_dmz_op(provider)
    plan = _dmz_plan(op, requires_slash32_armor=requires_slash32_armor)
    if not apply:
        # Dry-run: describe, do not mutate. No provider.set / reboot / runner /
        # armor.install is reached on this branch — the overnight-build guardrail.
        return IntentResult(plan=plan, applied=False, result=None)

    # The out-of-band gate is the start precondition (flip.gate_ok). It is checked
    # BEFORE guarded_apply takes a snapshot or fires anything, so a refusal makes
    # zero device writes.
    if not flip.gate_ok(out_of_band_reachable):
        result = OpResult(
            ok=False,
            detail=(
                "refused: no out-of-band recovery path — a single-NAT cutover that "
                "drops the WAN could strand the household with no way back in"
            ),
            before=provider.brand,
            after=None,
        )
        return IntentResult(plan=plan, applied=False, result=result)

    # Gate passed + apply: resolve the armor installer. A caller (CLI/test) may
    # inject one; otherwise build the real installer NOW (lazily, never on the
    # dry-run/gate-refused paths above) so the cutover actually deploys the kit
    # without every call site hand-building the seam. Constructed via the module-
    # level name so a test can swap it for a recording double (the wiring test).
    resolved_armor = armor if armor is not None else _default_armor_installer()

    verifiers = stage_verifiers or {}

    # FIX-e: a non-Bell ISP's passthrough yields a NORMAL public lease (no Bell /1
    # poison), so the self-healing /32 armor is unnecessary — skip the deploy
    # (``stage_armor``) and the post-cutover confirm (``apply_armor``) entirely. The
    # poison gate (above) is relaxed to accept any public prefix for the same flag.
    skipped_stages = (
        frozenset()
        if requires_slash32_armor
        else frozenset({"stage_armor", "apply_armor"})
    )

    def change(pv: DeviceProvider) -> None:
        # Walk the pure stage machine, firing each stage's I/O + verify. A
        # _StageError (or any provider/transport raise) propagates to
        # guarded_apply, which trips the rollback rails. ``pv`` is the wrapped
        # provider the rails pass us; we drive the SAME provider's ops through it.
        done: list[str] = []
        last_ok = True
        # Bound the loop by the stage count + 1 so a logic error can never spin.
        for _ in range(len(flip.FLIP_STAGES) + 1):
            nxt = flip.next_stage(done, last_ok=last_ok)
            if nxt is None:
                return
            if nxt == flip.ROLLBACK:  # defensive — last_ok is only ever True here
                msg = "flip machine forked to ROLLBACK"
                raise _StageError(msg)
            if nxt in skipped_stages:
                # This ISP needs no /32 armor — record the stage satisfied and advance
                # (the pure next_stage yields the first stage NOT in ``done``, so the
                # skipped stage must be marked done to move on) WITHOUT any host contact.
                done.append(nxt)
                continue
            # FIX-3: the fail-closed prevent-interlock fires AT the DMZ-engage moment
            # — immediately before the irreversible set() — re-sampling (OOB live,
            # armor staged, rollback staged) right now. A refusal raises before the
            # DMZ is ever touched, so guarded_apply unwinds on the disengaged baseline.
            if nxt in flip.INTERLOCKED_STAGES:
                _assert_interlock(
                    pv,
                    done,
                    oob_probe=oob_probe,
                    out_of_band_reachable=out_of_band_reachable,
                    requires_slash32_armor=requires_slash32_armor,
                )
            verifier = verifiers.get(nxt, _default_stage_verifier)
            _run_stage(
                nxt,
                provider=pv,
                op=op,
                runner=runner,
                armor=resolved_armor,
                verifier=verifier,
                requires_slash32_armor=requires_slash32_armor,
            )
            done.append(nxt)

    # Wrap the provider so the rails' rollback disables DMZ AND re-leases DHCP.
    guarded_provider = _DmzRollbackProvider(provider, runner)
    result = guarded_apply(
        guarded_provider,  # type: ignore[arg-type]  # structural DeviceProvider via delegation
        change,
        # The terminal end-to-end reachability check rides as the ``verify`` stage
        # inside the walk; the rails' own verify_fn therefore only needs to confirm
        # the walk completed (it raises on any failure), so a True here commits.
        verify_fn=lambda: True,
        confirm=confirm or _default_confirm,
        force=force,
        rollback=rollback,
        log_path=log_path,
    )
    return IntentResult(plan=plan, applied=True, result=result)
