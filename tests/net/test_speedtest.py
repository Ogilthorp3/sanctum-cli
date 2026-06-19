from __future__ import annotations

from sanctum_cli.net import speedtest

# ─── mbps_to_gbps ────────────────────────────────────────────────────


def test_mbps_to_gbps_converts() -> None:
    assert speedtest.mbps_to_gbps(1000) == 1.0
    assert speedtest.mbps_to_gbps(2500) == 2.5


def test_mbps_to_gbps_none_passthrough() -> None:
    assert speedtest.mbps_to_gbps(None) is None


# ─── find_bottleneck ─────────────────────────────────────────────────


def test_find_bottleneck_slowest_wired_hop_wins() -> None:
    hops = (("router port", 10000), ("switch", 1000), ("uplink", 2500))
    label, ceiling = speedtest.find_bottleneck(hops=hops, on_wifi=False)
    assert "switch" in label
    assert ceiling == 1.0


def test_find_bottleneck_ignores_none_links() -> None:
    hops = (("router port", None), ("switch", 2500), ("uplink", None))
    label, ceiling = speedtest.find_bottleneck(hops=hops, on_wifi=False)
    assert "switch" in label
    assert ceiling == 2.5


def test_find_bottleneck_wifi_sets_two_gbps_ceiling() -> None:
    hops = (("router port", 10000),)
    label, ceiling = speedtest.find_bottleneck(hops=hops, on_wifi=True)
    assert "Wi-Fi" in label
    assert ceiling == 2.0


def test_find_bottleneck_slower_wired_beats_wifi() -> None:
    # A 1 Gbps wired hop is slower than the ~2 Gbps Wi-Fi assumption -> wired wins.
    hops = (("old switch", 1000),)
    label, ceiling = speedtest.find_bottleneck(hops=hops, on_wifi=True)
    assert "switch" in label
    assert ceiling == 1.0


def test_find_bottleneck_empty_hops_unknown() -> None:
    label, ceiling = speedtest.find_bottleneck(hops=(), on_wifi=False)
    assert ceiling is None
    assert "unknown" in label.lower()


def test_find_bottleneck_empty_hops_wifi_still_two_gbps() -> None:
    label, ceiling = speedtest.find_bottleneck(hops=(), on_wifi=True)
    assert ceiling == 2.0
    assert "Wi-Fi" in label


# ─── classify_throughput ─────────────────────────────────────────────


def _advice_text(advice: tuple[str, ...]) -> str:
    return " ".join(advice).lower()


def test_classify_single_stream_artifact_named() -> None:
    # single 1.8, multi 7.9 -> multi >= 1.5x single AND single <= 2
    verdict, advice = speedtest.classify_throughput(
        multi_gbps=7.9, single_gbps=1.8, ceiling_gbps=10.0, on_wifi=False
    )
    text = (verdict + " " + _advice_text(advice)).lower()
    assert "artifact" in text
    assert "multi" in text


def test_classify_always_includes_nat_and_hub_rules() -> None:
    _, advice = speedtest.classify_throughput(
        multi_gbps=5.0, single_gbps=4.5, ceiling_gbps=10.0, on_wifi=False
    )
    text = _advice_text(advice)
    assert "nat" in text
    assert "under-report" in text or "underreport" in text or "under report" in text


def test_classify_on_wifi_advice() -> None:
    _, advice = speedtest.classify_throughput(
        multi_gbps=1.6, single_gbps=1.5, ceiling_gbps=2.0, on_wifi=True
    )
    text = _advice_text(advice)
    assert "wi-fi" in text or "wifi" in text
    assert "wired" in text


def test_classify_at_ceiling_says_fix_the_hop() -> None:
    verdict, advice = speedtest.classify_throughput(
        multi_gbps=2.45,
        single_gbps=2.4,
        ceiling_gbps=2.5,
        on_wifi=False,
        bottleneck="2.5 GbE switch",
    )
    text = (verdict + " " + _advice_text(advice)).lower()
    assert "ceiling" in text
    assert "2.5 gbe switch" in text


def test_classify_pppoe_rule_present_when_indicated() -> None:
    _, advice = speedtest.classify_throughput(
        multi_gbps=3.1, single_gbps=2.9, ceiling_gbps=8.0, on_wifi=False, pppoe=True
    )
    text = _advice_text(advice)
    assert "pppoe" in text


def test_classify_pppoe_rule_absent_when_not_indicated() -> None:
    _, advice = speedtest.classify_throughput(
        multi_gbps=5.0, single_gbps=4.8, ceiling_gbps=8.0, on_wifi=False, pppoe=False
    )
    text = _advice_text(advice)
    assert "pppoe" not in text


def test_classify_inconclusive_says_floor_not_truth() -> None:
    verdict, advice = speedtest.classify_throughput(
        multi_gbps=0.4,
        single_gbps=0.2,
        ceiling_gbps=10.0,
        on_wifi=False,
        test_inconclusive=True,
    )
    text = (verdict + " " + _advice_text(advice)).lower()
    assert "floor" in text
    assert "endpoint" in text
