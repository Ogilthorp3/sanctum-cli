from __future__ import annotations

from sanctum_cli.net import verify
from sanctum_cli.net.types import Verdict
from tests.net import fixtures as fx


def test_verify_single_nat_is_verified() -> None:
    v, reason = verify.verify(runner=fx.FakeRunner(fx.SINGLE_NAT))
    assert v is Verdict.VERIFIED
    assert "single" in reason.lower()


def test_verify_double_nat_is_not_yet() -> None:
    v, _ = verify.verify(runner=fx.FakeRunner(fx.DOUBLE_NAT))
    assert v is Verdict.NOT_YET


def test_verify_apipa_is_rollback() -> None:
    v, reason = verify.verify(runner=fx.FakeRunner(fx.APIPA))
    assert v is Verdict.APIPA_ROLLBACK
    assert "169.254" in reason or "apipa" in reason.lower()


def test_verify_unknown_is_inconclusive() -> None:
    blank = fx.FakeRunner({})
    v, _ = verify.verify(runner=blank)
    assert v is Verdict.INCONCLUSIVE


# ── Bell path-MTU (1492) black-hole classifier ──────────────────────────────


def test_classify_mtu_blackhole_when_wan_left_at_1500() -> None:
    # Large DF ping fails, small DF ping ok → path MTU is ~1492 but WAN is 1500.
    v, reason = verify.classify_mtu(wan_mtu=1500, big_df_ok=False, small_df_ok=True)
    assert v is Verdict.NOT_YET
    low = reason.lower()
    assert "1492" in reason
    assert "black-hole" in low or "black hole" in low


def test_classify_mtu_blackhole_when_wan_unknown() -> None:
    v, reason = verify.classify_mtu(wan_mtu=None, big_df_ok=False, small_df_ok=True)
    assert v is Verdict.NOT_YET
    assert "1492" in reason


def test_classify_mtu_verified_when_set_to_1492() -> None:
    v, reason = verify.classify_mtu(wan_mtu=1492, big_df_ok=False, small_df_ok=True)
    assert v is Verdict.VERIFIED
    assert "1492" in reason


def test_classify_mtu_fine_when_big_df_ok() -> None:
    # No black hole: large packets pass, so MTU is fine regardless of wan_mtu.
    v, _ = verify.classify_mtu(wan_mtu=1500, big_df_ok=True, small_df_ok=True)
    assert v is Verdict.VERIFIED


def test_classify_mtu_inconclusive_when_no_df_signal() -> None:
    # Both DF pings fail → can't tell path MTU; don't assert a black hole.
    v, _ = verify.classify_mtu(wan_mtu=1500, big_df_ok=False, small_df_ok=False)
    assert v is Verdict.INCONCLUSIVE
