"""Tests for the soak harness classifier (Task 6, Part 1).

Covers the four dirty conditions:
 1. Non-empty faults list.
 2. Any sample with red_probes.
 3. Any sample with service_nonzero.
 4. A pressure_level==4 sample not followed by a strictly-later pressure_level<=2 sample.
"""
from sanctum_cli.soak.harness import Sample, SoakResult, classify_soak


def _r(samples: list[Sample], faults: list[str] | None = None) -> SoakResult:
    return SoakResult(
        module="m",
        started_at="2026-06-01T00:00:00Z",
        last_at="2026-06-08T00:00:00Z",
        samples=samples,
        faults=faults or [],
    )


def test_clean_seven_day_soak() -> None:
    s = [Sample(ts="t", pressure_level=1, swap_used_mb=100, red_probes=[], service_nonzero=[])]
    days, clean = classify_soak(_r(s))
    assert clean is True and days >= 7.0


def test_red_probe_marks_dirty() -> None:
    s = [Sample(ts="t", pressure_level=1, swap_used_mb=100, red_probes=["m.p"], service_nonzero=[])]
    _, clean = classify_soak(_r(s))
    assert clean is False


def test_service_nonzero_marks_dirty() -> None:
    s = [Sample(ts="t", pressure_level=1, swap_used_mb=100, red_probes=[], service_nonzero=["com.sanctum.x"])]
    _, clean = classify_soak(_r(s))
    assert clean is False


def test_unrecovered_critical_pressure_marks_dirty() -> None:
    s = [Sample(ts="t1", pressure_level=4, swap_used_mb=9000, red_probes=[], service_nonzero=[])]
    _, clean = classify_soak(_r(s))
    assert clean is False  # critical pressure never followed by a normal sample


def test_faults_list_marks_dirty() -> None:
    s = [Sample(ts="t", pressure_level=1, swap_used_mb=100, red_probes=[], service_nonzero=[])]
    _, clean = classify_soak(_r(s, faults=["probe m.p failed at 2026-06-03"]))
    assert clean is False


def test_recovered_critical_pressure_is_clean() -> None:
    """Critical pressure followed by a recovery sample (pressure<=2) should be clean."""
    samples = [
        Sample(ts="t1", pressure_level=4, swap_used_mb=9000, red_probes=[], service_nonzero=[]),
        Sample(ts="t2", pressure_level=1, swap_used_mb=100, red_probes=[], service_nonzero=[]),
    ]
    _, clean = classify_soak(_r(samples))
    assert clean is True


def test_days_computed_from_started_and_last() -> None:
    """Days = (last_at - started_at) in floating-point days."""
    result = SoakResult(
        module="m",
        started_at="2026-06-01T00:00:00Z",
        last_at="2026-06-04T12:00:00Z",  # 3.5 days later
        samples=[Sample(ts="t", pressure_level=1, swap_used_mb=0, red_probes=[], service_nonzero=[])],
        faults=[],
    )
    days, _ = classify_soak(result)
    assert abs(days - 3.5) < 0.01
