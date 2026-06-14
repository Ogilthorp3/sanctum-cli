from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.net import detect
from sanctum_cli.net.types import Nat, Verdict

if TYPE_CHECKING:
    from sanctum_cli.net.detect import Runner


def verify(*, runner: Runner) -> tuple[Verdict, str]:
    """Local-only single-NAT verification (survives WAN loss by reading local state)."""
    wan_ip = runner(("fw_wan_ip",)).strip() or None
    hop2 = detect.parse_hop2(runner(("traceroute",)))

    if detect.is_apipa(wan_ip):
        return Verdict.APIPA_ROLLBACK, f"WAN is APIPA ({wan_ip}) — DHCP failed; roll back."
    nat = detect.classify_nat(hop2=hop2, wan_ip=wan_ip)
    if nat is Nat.SINGLE:
        return Verdict.VERIFIED, "Single-NAT confirmed (Firewalla holds a public WAN path)."
    if nat is Nat.DOUBLE:
        return Verdict.NOT_YET, "Still double-NAT — not cut over yet (try the WAN bounce again)."
    return Verdict.INCONCLUSIVE, "Could not confirm — check the Firewalla app's WAN IP."
