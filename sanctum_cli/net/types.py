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


@dataclass(frozen=True)
class Baseline:
    wan_ip: str | None
    gateway_ip: str | None
    public_ip: str | None
    mtu: int | None
