"""Pure ship-bar gate functions. Side effects are injected so each gate is
unit-testable against hostile inputs (dead sink, false-green probe, missing
secret) without touching the real haus."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from sanctum_cli.modules.manifest import ModuleManifest


class GateStatus(StrEnum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass
class GateResult:
    name: str
    status: GateStatus
    detail: str


def gate_install_uninstall(m: ModuleManifest) -> GateResult:
    # The former amber branch (`remove_paths and not rename_suffix`) was unreachable:
    # the RED check above already returns when `rename_suffix` is falsy, so the
    # amber condition could never be True. Removed.
    if not m.uninstall.rename_suffix:
        return GateResult("install/uninstall", GateStatus.RED, "no uninstall handler")
    return GateResult("install/uninstall", GateStatus.GREEN, "reversible, data preserved")


def gate_secrets(
    m: ModuleManifest,
    keychain_has: Callable[[str, str], bool],
    is_default: Callable[[str, str], bool],
) -> GateResult:
    missing = [s.service for s in m.secrets
               if s.required and not keychain_has(s.account, s.service)]
    if missing:
        return GateResult("secrets-bootstrap", GateStatus.RED,
                          f"missing required secrets: {missing}")
    defaulted = [s.service for s in m.secrets if is_default(s.account, s.service)]
    if defaulted:
        return GateResult("secrets-bootstrap", GateStatus.AMBER,
                          f"secrets look like Bert-defaults: {defaulted}")
    return GateResult("secrets-bootstrap", GateStatus.GREEN, "present + non-default")


def gate_self_heal(
    m: ModuleManifest,
    heal_action_ok: Callable[[str], bool],
) -> GateResult:
    keepalive = [s for s in m.services if s.keepalive]
    no_probe = [s.label for s in keepalive if not s.health_probe]
    if no_probe:
        return GateResult("self-heal", GateStatus.RED,
                          f"keepalive services without a health probe: {no_probe}")
    crashing = [s.label for s in keepalive
                if s.health_probe and not heal_action_ok(s.label)]
    if crashing:
        return GateResult("self-heal", GateStatus.RED,
                          f"heal action missing/crashing: {crashing}")
    if not keepalive:
        return GateResult("self-heal", GateStatus.GREEN, "no long-running services")
    return GateResult("self-heal", GateStatus.AMBER, "heal wired, soak-unproven")


def gate_alert_hygiene(
    m: ModuleManifest,
    sink_live: Callable[[str], bool],
    probe_is_false_green: Callable[[str], bool],
) -> GateResult:
    if not sink_live(m.alerts.sink):
        return GateResult("alert-hygiene", GateStatus.RED,
                          f"alert sink '{m.alerts.sink}' is not reachable")
    liars = [p for p in m.probes if probe_is_false_green(p)]
    if liars:
        return GateResult("alert-hygiene", GateStatus.RED,
                          f"false-green probes (report ok while failing): {liars}")
    if len(m.alerts.pager_conditions) > 3:
        return GateResult("alert-hygiene", GateStatus.AMBER,
                          "pager conditions look broad; keep P0/P1 crucial-only")
    return GateResult("alert-hygiene", GateStatus.GREEN, "live sink, minimal pager")


def gate_soak(
    m: ModuleManifest,
    soak_days: Callable[[ModuleManifest], float | None],
    soak_clean: Callable[[ModuleManifest], bool],
) -> GateResult:
    days = soak_days(m)
    if days is None:
        return GateResult("soak", GateStatus.RED, "no soak result recorded")
    if not soak_clean(m):
        return GateResult("soak", GateStatus.RED, f"soak recorded faults ({days:.1f}d)")
    if days < m.soak.min_days:
        return GateResult("soak", GateStatus.AMBER,
                          f"soak {days:.1f}d < required {m.soak.min_days}d")
    return GateResult("soak", GateStatus.GREEN, f"clean {days:.1f}d soak")


def gate_docs_demo(
    m: ModuleManifest,
    docs_resolves: Callable[[str], bool],
    demo_exits_zero: Callable[[str], bool],
) -> GateResult:
    ok_docs = docs_resolves(m.docs)
    ok_demo = demo_exits_zero(m.demo)
    if ok_docs and ok_demo:
        return GateResult("docs+demo", GateStatus.GREEN, "docs resolve, demo exits 0")
    if ok_docs or ok_demo:
        return GateResult("docs+demo", GateStatus.AMBER,
                          f"docs={'ok' if ok_docs else 'X'} demo={'ok' if ok_demo else 'X'}")
    return GateResult("docs+demo", GateStatus.RED, "neither docs nor demo verified")


def overall(results: list[GateResult]) -> GateStatus:
    if any(r.status is GateStatus.RED for r in results):
        return GateStatus.RED
    if any(r.status is GateStatus.AMBER for r in results):
        return GateStatus.AMBER
    return GateStatus.GREEN
