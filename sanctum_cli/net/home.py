"""Sanctum Home Network — product-facing health for dogfooding.

Apple-style surface: one glance (GREEN / ATTENTION / DEGRADED) and one safe
action path (``improve``). Pure assembly lives here; impure probes live in the
CLI handler so unit tests never need a live Firewalla.

Doctrine:
* Never strand the haus — improve only applies reversible, fail-safe fixes
  (MSS clamp, mtu probing, armor assert). ADMZ cutover is *preflight only*
  unless explicitly armed elsewhere.
* UNKNOWN probes fail-open for overall (cannot false-alarm DEGRADED).
* DOWN = continuous protection or internet path broken *now*.
* ATTENTION = works now, better path available (e.g. ADMZ ready, speed class).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Health(Enum):
    OK = "ok"
    ATTENTION = "attention"
    DOWN = "down"
    UNKNOWN = "unknown"


class Overall(Enum):
    GREEN = "GREEN"
    ATTENTION = "ATTENTION"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class Probe:
    """One product row input (already probed)."""

    label: str
    health: Health
    detail: str
    action: str | None = None  # optional next step for the user


@dataclass(frozen=True)
class HomeReport:
    """Assembled home-network product pane."""

    overall: Overall
    headline: str
    rows: tuple[Probe, ...]
    next_steps: tuple[str, ...]
    improve_safe: bool  # True if `improve` can apply safe fixes
    improve_detail: str


@dataclass(frozen=True)
class InternetPath:
    """TLS path samples: Fastly (CDN/PyPI) + control (Google)."""

    fastly_ok: bool | None  # None = probe failed
    control_ok: bool | None
    detail: str


@dataclass(frozen=True)
class WanPath:
    """How the house reaches the internet."""

    # "pppoe" | "public_eth" | "private" | "unknown"
    kind: str
    public_ip: str | None
    wan_if: str | None
    detail: str


@dataclass(frozen=True)
class MssGuard:
    """Firewalla MSS clamp + mtu probing (Bell PPPoE CDN fix)."""

    mss_ok: bool | None  # True if 1400 clamp present
    probing_ok: bool | None
    detail: str


@dataclass(frozen=True)
class ArmorState:
    """singlenat-armor rollup."""

    state: str | None  # HEALTHY | DEGRADED_* | None
    singlenat: bool | None
    poison: bool | None
    detail: str


@dataclass(frozen=True)
class HubReach:
    """Bell/GigaHub management reachability (for optional ADMZ)."""

    reachable: bool | None
    host: str | None
    detail: str


def _internet_row(path: InternetPath | None) -> Probe:
    if path is None:
        return Probe("Internet", Health.UNKNOWN, "probe unavailable")
    if path.fastly_ok is True and path.control_ok is not False:
        return Probe("Internet", Health.OK, path.detail or "TLS paths healthy")
    if path.fastly_ok is False and path.control_ok is True:
        return Probe(
            "Internet",
            Health.DOWN,
            path.detail or "CDN/TLS path broken (Fastly) while general HTTPS works",
            action="Run: sanctum net home improve  # re-assert MSS 1400 on Firewalla",
        )
    if path.control_ok is False:
        return Probe(
            "Internet",
            Health.DOWN,
            path.detail or "General HTTPS broken",
            action="Check modem/Firewalla WAN; avoid WAN surgery mid-call",
        )
    return Probe("Internet", Health.UNKNOWN, path.detail or "incomplete probe")


def _wan_row(wan: WanPath | None) -> Probe:
    if wan is None:
        return Probe("WAN path", Health.UNKNOWN, "probe unavailable")
    if wan.kind == "pppoe":
        return Probe(
            "WAN path",
            Health.OK,
            wan.detail or f"PPPoE single-NAT · {wan.public_ip or '?'}",
        )
    if wan.kind == "public_eth":
        return Probe(
            "WAN path",
            Health.OK,
            wan.detail or f"Public DHCP (ADMZ-class) · {wan.public_ip or '?'}",
        )
    if wan.kind == "private":
        return Probe(
            "WAN path",
            Health.ATTENTION,
            wan.detail or "Double-NAT / private WAN — optimizable",
            action="Optional later: sanctum net single-nat --check (after hub reachable)",
        )
    return Probe("WAN path", Health.UNKNOWN, wan.detail or "unknown WAN class")


def _mss_row(mss: MssGuard | None) -> Probe:
    if mss is None:
        return Probe("CDN guard", Health.UNKNOWN, "Firewalla unreachable / no key")
    if mss.mss_ok is True:
        detail = mss.detail or "MSS 1400 + mtu_probing (Fastly/PyPI safe)"
        if mss.probing_ok is False:
            return Probe(
                "CDN guard",
                Health.ATTENTION,
                detail + " · tcp_mtu_probing off",
                action="sanctum net home improve",
            )
        return Probe("CDN guard", Health.OK, detail)
    if mss.mss_ok is False:
        return Probe(
            "CDN guard",
            Health.DOWN,
            mss.detail or "MSS 1400 clamp missing — CDN TLS may hang",
            action="sanctum net home improve",
        )
    return Probe("CDN guard", Health.UNKNOWN, mss.detail or "could not read iptables")


def _armor_row(armor: ArmorState | None) -> Probe:
    if armor is None:
        return Probe("Armor", Health.UNKNOWN, "probe unavailable")
    st = (armor.state or "").upper()
    if st == "HEALTHY" and armor.poison is not True:
        return Probe("Armor", Health.OK, armor.detail or "singlenat armor HEALTHY")
    if armor.poison is True:
        return Probe(
            "Armor",
            Health.DOWN,
            armor.detail or "Bell /1 poison route present",
            action="Armor heal / check singlenat-watchdog",
        )
    if st.startswith("DEGRADED") or st == "DARK":
        return Probe("Armor", Health.DOWN, armor.detail or st)
    return Probe("Armor", Health.ATTENTION, armor.detail or (st or "unknown"))


def _hub_row(hub: HubReach | None) -> Probe:
    if hub is None:
        return Probe("Hub (optional)", Health.UNKNOWN, "probe unavailable")
    if hub.reachable is True:
        return Probe(
            "Hub (optional)",
            Health.ATTENTION,
            hub.detail or f"Reachable at {hub.host} — speed upgrade preflight possible",
            action="Speed upgrade is optional; only after Zoom/quiet window",
        )
    if hub.reachable is False:
        return Probe(
            "Hub (optional)",
            Health.OK,  # not required for a healthy home
            hub.detail
            or "Not on this LAN (normal under PPPoE bridge) — ADMZ needs dual-home later",
        )
    return Probe("Hub (optional)", Health.UNKNOWN, hub.detail or "unknown")


def build_home_report(
    *,
    internet: InternetPath | None,
    wan: WanPath | None,
    mss: MssGuard | None,
    armor: ArmorState | None,
    hub: HubReach | None,
) -> HomeReport:
    """PURE: map probe results → product pane + overall + next steps."""
    rows = (
        _internet_row(internet),
        _wan_row(wan),
        _mss_row(mss),
        _armor_row(armor),
        _hub_row(hub),
    )
    statuses = {r.health for r in rows}
    if Health.DOWN in statuses:
        overall = Overall.DEGRADED
        headline = "Home network needs attention — something is broken right now."
    elif Health.ATTENTION in statuses:
        overall = Overall.ATTENTION
        headline = "Home network is working — optional improvements available."
    else:
        overall = Overall.GREEN
        headline = "Home network is protected and online."

    steps: list[str] = []
    for r in rows:
        if r.action and r.health in (Health.DOWN, Health.ATTENTION):
            steps.append(r.action)
    # de-dupe preserve order
    seen: set[str] = set()
    next_steps = tuple(s for s in steps if not (s in seen or seen.add(s)))  # type: ignore[func-returns-value]

    # Safe improve: only when CDN guard missing or probing off (never ADMZ here)
    improve_safe = False
    improve_detail = "Nothing safe to auto-fix (or Firewalla unreachable)."
    if mss is not None and (mss.mss_ok is False or mss.probing_ok is False):
        improve_safe = True
        improve_detail = "Will re-assert Firewalla MSS 1400 + tcp_mtu_probing (no WAN mode change)."
    elif internet is not None and internet.fastly_ok is False and internet.control_ok is True:
        improve_safe = True
        improve_detail = "CDN TLS broken with HTTPS OK — will re-assert MSS 1400 guard."

    return HomeReport(
        overall=overall,
        headline=headline,
        rows=rows,
        next_steps=next_steps,
        improve_safe=improve_safe,
        improve_detail=improve_detail,
    )
