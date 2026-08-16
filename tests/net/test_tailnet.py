"""Tests for the pure ``sanctum tailnet doctor`` assembler + classifiers.

Pure functions over already-probed values → row statuses + an OVERALL verdict.
No live calls; the CLI handler (tests/test_tailnet_cli.py) does the impure probing.
These pin the truth table — most importantly the load-bearing ACL-gap signal
(disco ping OK + TCP :22 filtered ⇒ the policy, not the host) and the dead-cred
(401) case — plus the fail-open UNKNOWN behaviour.
"""

from __future__ import annotations

from sanctum_cli.net.status import RowStatus
from sanctum_cli.net.tailnet import (
    AclDrift,
    CredState,
    PeerReach,
    SpineState,
    TrifectaState,
    build_tailnet_report,
    classify_cred,
    classify_reachability,
    diff_acl,
)

# ─── classify_reachability ────────────────────────────────────────────


def test_reachability_tcp_open_is_ok() -> None:
    status, detail = classify_reachability(ping_ok=True, tcp22_open=True)
    assert status is RowStatus.OK
    assert ":22 reachable" in detail


def test_reachability_ping_ok_tcp_filtered_is_acl_gap() -> None:
    """The signal this toolkit exists to name: overlay up, port filtered = ACL gap."""
    status, detail = classify_reachability(ping_ok=True, tcp22_open=False)
    assert status is RowStatus.DOWN
    assert "ACL gap" in detail
    assert "sanctum tailnet apply" in detail


def test_reachability_no_ping_no_tcp_is_unreachable() -> None:
    status, detail = classify_reachability(ping_ok=False, tcp22_open=False)
    assert status is RowStatus.DOWN
    assert "unreachable" in detail


def test_reachability_both_unprobed_is_unknown() -> None:
    status, _ = classify_reachability(ping_ok=None, tcp22_open=None)
    assert status is RowStatus.UNKNOWN


# ─── classify_cred ────────────────────────────────────────────────────


def test_cred_200_is_ok() -> None:
    status, _ = classify_cred(200)
    assert status is RowStatus.OK


def test_cred_401_is_down_with_creds_hint() -> None:
    status, detail = classify_cred(401)
    assert status is RowStatus.DOWN
    assert "sanctum tailnet creds" in detail


def test_cred_zero_is_down() -> None:
    status, detail = classify_cred(0)
    assert status is RowStatus.DOWN
    assert "could not authenticate" in detail


def test_cred_none_is_unknown() -> None:
    status, _ = classify_cred(None)
    assert status is RowStatus.UNKNOWN


def test_cred_unexpected_is_attention() -> None:
    status, _ = classify_cred(503)
    assert status is RowStatus.ATTENTION


# ─── diff_acl ─────────────────────────────────────────────────────────


def test_diff_acl_ignores_comments_and_whitespace() -> None:
    local = '// a comment\n{\n  "acls": [],\n}\n'
    live = '{"acls":[]}'
    drift = diff_acl(local, live)
    assert drift.in_sync is True


def test_diff_acl_detects_real_difference() -> None:
    drift = diff_acl('{"acls":[]}', '{"acls":[{"action":"accept"}]}')
    assert drift.in_sync is False
    assert "differs" in drift.summary


# ─── build_tailnet_report (verdict reduction) ─────────────────────────


def test_report_all_none_is_green_and_all_unknown() -> None:
    """A pane where every probe failed still renders — UNKNOWN is fail-open."""
    report = build_tailnet_report(spine=None, peer=None, cred=None, drift=None, trifecta=None)
    assert report.overall == "GREEN"
    assert all(row.status is RowStatus.UNKNOWN for row in report.rows)
    assert len(report.rows) == 5


def test_report_down_row_makes_it_degraded() -> None:
    report = build_tailnet_report(
        spine=SpineState(on_tailnet=True, suffix="tail1a2b.ts.net"),
        peer=PeerReach(peer="berts-mbp", ping_ok=True, tcp22_open=False),  # ACL gap → DOWN
        cred=CredState(http_code=200, source="keychain oauth"),
        drift=AclDrift(in_sync=True, summary="matches"),
        trifecta=TrifectaState(keychain=True, onepassword=None, providers_row=True),
    )
    assert report.overall == "DEGRADED"


def test_report_attention_only_is_attention() -> None:
    report = build_tailnet_report(
        spine=SpineState(on_tailnet=True, suffix=""),
        peer=PeerReach(peer="berts-mbp", ping_ok=True, tcp22_open=True),  # OK
        cred=CredState(http_code=200, source="keychain oauth"),
        drift=AclDrift(in_sync=False, summary="drifted"),  # ATTENTION
        trifecta=TrifectaState(keychain=True, onepassword=None, providers_row=True),
    )
    assert report.overall == "ATTENTION"
