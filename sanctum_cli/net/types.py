from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Nat(StrEnum):
    SINGLE = "single"
    DOUBLE = "double"
    CGNAT = "cgnat"
    UNKNOWN = "unknown"


class Verdict(StrEnum):
    VERIFIED = "verified"
    NOT_YET = "not_yet"
    APIPA_ROLLBACK = "apipa_rollback"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class TopologyReport:
    firewalla_present: bool
    firewalla_wan_mac: str | None
    firewalla_wan_mtu: int | None
    nat: Nat
    gateway_ip: str | None
    isp: str
    public_ip: str | None
    applicable: bool
    reason: str
    wan_ip: str | None = None


@dataclass(frozen=True)
class Playbook:
    id: str
    display_name: str
    achieves: Literal["single_nat", "not_possible"]
    gateway_ips: tuple[str, ...]
    title_contains: tuple[str, ...]
    admin_url_template: str
    steps: tuple[str, ...]
    gotchas: tuple[str, ...]
    ordering: tuple[str, ...]
    rollback: tuple[str, ...]
    # Optional (defaults last so existing playbooks/tests keep working):
    # prechecks   — things to confirm BEFORE touching the box (e.g. LAN subnet)
    # mtu         — WAN MTU this ISP's path requires (None = leave default)
    # alt_playbook — id of an alternative method reached via this playbook
    # requires_slash32_armor — does this ISP's single-NAT passthrough hand the WAN a
    #   /1-poison public lease that needs the self-healing /32 armor (stage_armor +
    #   apply_armor) and the /32 poison gate? Bell's Advanced DMZ does; a generic /
    #   CGNAT ISP does not (its passthrough yields a normal public lease, so the armor
    #   stages are skipped and the poison gate accepts a healthy public lease of any
    #   prefix). Default True — the SAFE default: an unset/unknown playbook never skips
    #   the armor on what could be a real /1 lease (fail-safe, not fail-open).
    prechecks: tuple[str, ...] = ()
    mtu: int | None = None
    alt_playbook: str | None = None
    requires_slash32_armor: bool = True


@dataclass(frozen=True)
class Baseline:
    wan_ip: str | None
    gateway_ip: str | None
    public_ip: str | None
    mtu: int | None


@dataclass(frozen=True)
class SpeedReport:
    multi_gbps: float | None
    single_gbps: float | None
    ceiling_gbps: float | None
    on_wifi: bool | None
    hops: tuple[tuple[str, int | None], ...]
    bottleneck: str
    verdict: str
    advice: tuple[str, ...]
    test_inconclusive: bool = False
