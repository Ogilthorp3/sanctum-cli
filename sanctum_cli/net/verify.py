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


def classify_mtu(
    *, wan_mtu: int | None, big_df_ok: bool, small_df_ok: bool
) -> tuple[Verdict, str]:
    """Classify a path-MTU black hole from two DF-ping results.

    The caller probes with the don't-fragment bit set at two sizes: a ~1500-byte
    packet (`big_df_ok`) and a ~1492-byte packet (`small_df_ok`). On a Bell path
    the large one black-holes while the small one passes — ping works but HTTPS
    silently hangs — which means the WAN MTU must be 1492 (+ MSS clamp).

    Pure: no probing here, just the verdict from the two booleans + the WAN MTU.
    """
    if big_df_ok:
        # Large packets pass — no black hole regardless of the configured WAN MTU.
        return Verdict.VERIFIED, "No MTU black hole: large (DF) packets pass."
    if not small_df_ok:
        # Neither size passed — can't conclude a 1492 path MTU from this signal.
        return (
            Verdict.INCONCLUSIVE,
            "MTU inconclusive: both DF pings failed — check connectivity, not just MTU.",
        )
    # big fails, small passes → path MTU is ~1492.
    if wan_mtu == 1492:
        return Verdict.VERIFIED, "WAN MTU 1492 matches Bell's path MTU."
    return (
        Verdict.NOT_YET,
        "MTU black-hole risk: large packets fail; set WAN MTU to 1492 (+MSS clamp).",
    )
