from __future__ import annotations

# Pure, import-light throughput logic. No I/O here — the real probes live in
# system.py and feed these functions. Everything is deterministic and unit-
# testable so the "honest throughput doctor" interpretation is the tested
# contract, independent of any flaky network endpoint.

# Wi-Fi tops out well below a multi-gig wired line in practice. We treat ~2 Gbps
# as the realistic Wi-Fi ceiling (Wi-Fi 6/6E on a good AP); you cannot observe a
# faster line over Wi-Fi no matter how fast the line is.
_WIFI_CEILING_GBPS = 2.0

# "Close enough" band for deciding you're sitting on the path ceiling. Real
# tests never hit the link rate exactly (framing/overhead), so >=85% counts.
_AT_CEILING_FRAC = 0.85

# Single-stream artifact thresholds: a single TCP stream often stalls near
# 1-2 Gbps while N parallel streams reach the real line rate.
_SINGLE_STREAM_CAP_GBPS = 2.0
_MULTI_OVER_SINGLE = 1.5

# PPPoE caps CPU-bound routers around here; quoted so advice can name the band.
_PPPOE_BAND = "~3-3.5 Gbps"


def mbps_to_gbps(mbps: float | None) -> float | None:
    """Convert megabits/sec to gigabits/sec; None passes through."""
    if mbps is None:
        return None
    return round(mbps / 1000.0, 3)


def find_bottleneck(
    *,
    hops: tuple[tuple[str, int | None], ...],
    on_wifi: bool | None,
) -> tuple[str, float | None]:
    """Return (human label, ceiling Gbps) of the slowest *known* link in the path.

    Hops are (name, link-Mbps); links with an unknown (None) speed are ignored.
    If on Wi-Fi, the radio is treated as a ~2 Gbps cap and competes with the
    wired hops — whichever is slower wins (an old 1 GbE switch still beats the
    Wi-Fi assumption, so the wired hop is named). Empty + wired -> unknown.
    """
    known = [(name, mbps) for name, mbps in hops if mbps is not None]
    slowest_wired_gbps: float | None = None
    slowest_wired_label = ""
    if known:
        name, mbps = min(known, key=lambda h: h[1])
        slowest_wired_gbps = mbps_to_gbps(mbps)
        slowest_wired_label = f"{name} ({slowest_wired_gbps} Gbps link)"

    if on_wifi:
        # Wi-Fi competes with the slowest wired hop; slower link is the ceiling.
        if slowest_wired_gbps is not None and slowest_wired_gbps < _WIFI_CEILING_GBPS:
            return slowest_wired_label, slowest_wired_gbps
        return "Wi-Fi (~1-2 Gbps typical)", _WIFI_CEILING_GBPS

    if slowest_wired_gbps is not None:
        return slowest_wired_label, slowest_wired_gbps
    return "unknown (no link speeds detected)", None


def classify_throughput(
    *,
    multi_gbps: float | None,
    single_gbps: float | None,
    ceiling_gbps: float | None,
    on_wifi: bool | None,
    bottleneck: str = "your slowest hop",
    pppoe: bool = False,
    test_inconclusive: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Return (verdict, advice tuple) — the honest interpretation of the numbers.

    Encodes the hard-won field rules. The verdict is one plain-language line;
    advice is a bullet list. The NAT-irrelevant and hub-under-report rules are
    ALWAYS present; Wi-Fi / at-ceiling / single-stream-artifact / PPPoE /
    inconclusive notes are added when the inputs indicate them.
    """
    verdict = "Measured throughput looks consistent with your path."
    advice: list[str] = []

    best = _best(multi_gbps, single_gbps)

    if test_inconclusive:
        verdict = (
            "The live test was endpoint-limited, not network-limited — "
            "this number is a FLOOR, not the truth."
        )
        advice.append(
            "Few streams completed (rate-limited or a slow mirror). The real "
            "result is at least this fast; re-run, raise --streams, or install "
            "the Ookla `speedtest` CLI for a cleaner number."
        )
    elif (
        single_gbps is not None
        and multi_gbps is not None
        and single_gbps <= _SINGLE_STREAM_CAP_GBPS
        and multi_gbps >= _MULTI_OVER_SINGLE * single_gbps
    ):
        verdict = (
            "Your single-stream number was the artifact; the multi-stream result is closer to real."
        )
        advice.append(
            f"A single connection stalled near {single_gbps} Gbps while "
            f"{multi_gbps} Gbps flowed across parallel streams. Most built-in "
            "and one-click speed tests use a single stream and under-report a "
            "multi-gig line — trust the multi-stream figure."
        )
    elif best is not None and ceiling_gbps is not None and best >= _AT_CEILING_FRAC * ceiling_gbps:
        verdict = (
            f"You're hitting your path ceiling (the {bottleneck}); to go faster, fix that hop."
        )
        advice.append(
            f"Measured ~{best} Gbps against a ~{ceiling_gbps} Gbps ceiling. "
            "More streams or a different server will not help — the limit is "
            "the link, not the test."
        )

    if on_wifi:
        advice.append(
            "You are on Wi-Fi, which caps you around 1-2 Gbps regardless of "
            "the line behind it. You cannot see a faster line over Wi-Fi — "
            "go wired (plug into Ethernet) and re-test to measure the real ceiling."
        )

    # Always-on field rules.
    advice.append(
        "Single- vs double-NAT does NOT change throughput. NAT layers affect "
        "reachability/port-forwarding, not speed — do not chase NAT to go faster."
    )
    advice.append(
        "Your ISP's built-in / hub speed test usually under-reports: it is "
        "single-stream and runs on the gateway's weak CPU. Measure from your "
        "own machine with parallel streams instead."
    )

    if pppoe:
        advice.append(
            f"This WAN uses PPPoE, which is CPU-bound on most routers and caps "
            f"throughput around {_PPPOE_BAND}. If you pay for more than that and "
            "see a wall here, the router's PPPoE handling is the bottleneck."
        )

    return verdict, tuple(advice)


def _best(multi_gbps: float | None, single_gbps: float | None) -> float | None:
    """The faster of the two measured numbers (multi normally wins)."""
    vals = [v for v in (multi_gbps, single_gbps) if v is not None]
    return max(vals) if vals else None
