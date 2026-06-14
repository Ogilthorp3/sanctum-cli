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
