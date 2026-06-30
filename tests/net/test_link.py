"""Tests for the Sanctum Link diagnosis classifier — pure, no network.

Ported from /tmp/link-build/test_link_diagnose.py to the dataclass API
(``Sample`` / ``Diagnosis``) in ``sanctum_cli.net.link``.
"""

from __future__ import annotations

from sanctum_cli.net.link import Sample, classify, parse_log


def _s(avg: float, load: float, loss: float = 0.0, degraded: bool | None = None) -> Sample:
    flag = (avg > 20 or loss > 0) if degraded is None else degraded
    return Sample(
        min=3.0,
        avg=avg,
        max=avg * 3,
        std=avg,
        loss=loss,
        load=load,
        degraded=flag,
    )


def test_no_data() -> None:
    assert classify([]).verdict == "NO_DATA"


def test_healthy_low_latency() -> None:
    s = [_s(5, 2.0), _s(6, 2.1), _s(4, 1.9), _s(7, 2.2)]
    assert classify(s).verdict == "HEALTHY"


def test_radio_when_loss_present() -> None:
    # Loss > 1% means the radio itself is dropping frames, regardless of load.
    s = [_s(8, 2.0, loss=3.0), _s(9, 2.1, loss=2.0), _s(7, 2.0, loss=4.0)]
    assert classify(s).verdict == "RADIO"


def test_load_when_latency_tracks_load() -> None:
    # Latency rises monotonically with load, zero loss -> LOAD/CAPACITY.
    s = [_s(5, 2.5, degraded=False), _s(22, 3.4), _s(43, 4.5), _s(80, 5.5), _s(110, 6.5)]
    r = classify(s)
    assert r.verdict == "LOAD"
    assert "WIRED" in r.remedy


def test_scan_when_degraded_but_uncorrelated_with_load() -> None:
    # Periodic latency spikes at CONSTANT low load -> off-channel scanning.
    s = [
        _s(5, 2.0, degraded=False),
        _s(60, 2.0),
        _s(5, 2.0, degraded=False),
        _s(70, 2.0),
        _s(5, 2.0, degraded=False),
        _s(65, 2.0),
    ]
    assert classify(s).verdict == "SCAN"


def test_parse_real_sentinel_line() -> None:
    line = (
        "2026-06-29T21:25:35 ssid=<redacted> "
        "rtt=4.748/107.547/520.332/138.349 loss=0.0% "
        "load=[6.48 4.39 3.89] DEGRADED"
    )
    got = parse_log(line)
    assert len(got) == 1
    assert got[0].avg == 107.547
    assert got[0].load == 6.48
    assert got[0].loss == 0.0
    assert got[0].degraded is True


def test_parse_skips_garbage_lines() -> None:
    assert parse_log("hello\n\n# comment") == []


def test_reference_mini_dataset_is_load_bound() -> None:
    # The actual 8-sample window from the Mini (2026-06-29) must classify LOAD.
    raw = """\
2026-06-29T21:07:03 ssid=x rtt=2.479/34.863/106.761/36.142 loss=0.0% load=[3.19 3.28 3.17] DEGRADED
2026-06-29T21:07:57 ssid=x rtt=2.837/53.026/144.474/56.332 loss=0.0% load=[3.17 3.27 3.17] DEGRADED
2026-06-29T21:10:09 ssid=x rtt=2.531/5.408/13.028/2.637 loss=0.0% load=[2.84 3.19 3.15] ok
2026-06-29T21:13:14 ssid=x rtt=3.425/35.831/164.101/53.661 loss=0.0% load=[3.58 3.86 3.51] DEGRADED
2026-06-29T21:16:19 ssid=x rtt=3.217/42.700/122.192/45.775 loss=0.0% load=[4.49 4.10 3.66] DEGRADED
2026-06-29T21:19:24 ssid=x rtt=2.589/30.183/175.012/48.336 loss=0.0% load=[3.87 4.10 3.75] DEGRADED
2026-06-29T21:22:29 ssid=x rtt=3.240/22.226/78.362/23.976 loss=0.0% load=[3.42 3.66 3.62] DEGRADED
2026-06-29T21:25:35 ssid=x rtt=4.748/107.547/520.332/138.349 loss=0.0% load=[6.48 4.39 3.89] DEGRADED
"""
    assert classify(parse_log(raw)).verdict == "LOAD"
