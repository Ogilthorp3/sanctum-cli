"""Tests for ``sanctum self-test`` — Phase 2 Task 2.3 of Family Pass v1.0.

The command runs a fleet of probes. We don't want to depend on a real
running Sanctum during unit tests, so we patch the probe registry to a
controlled set + verify the runner's contract:

  - per-probe lines render
  - the summary panel renders
  - exit code is 0 iff every probe passed, 1 if any failed
  - --json emits machine-readable output
  - --only filters by substring
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import self_test as st


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def all_pass_probes(monkeypatch):
    """Force every probe in the registry to return passed=True."""
    fake = [
        st.Probe("alpha probe", lambda: st.ProbeResult(True, "ok")),
        st.Probe("beta probe", lambda: st.ProbeResult(True, "ok")),
        st.Probe("gamma probe", lambda: st.ProbeResult(True, "ok")),
    ]
    monkeypatch.setattr(st, "PROBES", fake)
    return fake


@pytest.fixture
def one_fail_probes(monkeypatch):
    fake = [
        st.Probe("alpha probe", lambda: st.ProbeResult(True, "ok")),
        st.Probe("beta probe", lambda: st.ProbeResult(False, "intentional test failure")),
        st.Probe("gamma probe", lambda: st.ProbeResult(True, "ok")),
    ]
    monkeypatch.setattr(st, "PROBES", fake)
    return fake


def test_all_pass_exits_zero(runner, all_pass_probes):
    result = runner.invoke(app, ["self-test"])
    assert result.exit_code == 0
    assert "alpha probe" in result.output
    assert "beta probe" in result.output
    assert "gamma probe" in result.output


def test_all_pass_panel_says_healthy(runner, all_pass_probes):
    result = runner.invoke(app, ["self-test"])
    # Headline is tier-aware: "Sanctum CLI is healthy" or "Sanctum haus is healthy".
    # Either form indicates a passing run.
    assert "is healthy" in result.output
    assert "3/3" in result.output


def test_one_fail_exits_one(runner, one_fail_probes):
    result = runner.invoke(app, ["self-test"])
    assert result.exit_code == 1


def test_one_fail_panel_says_failed(runner, one_fail_probes):
    result = runner.invoke(app, ["self-test"])
    assert "failed" in result.output
    assert "intentional test failure" in result.output


def test_json_output_all_pass(runner, all_pass_probes):
    result = runner.invoke(app, ["self-test", "--json"])
    assert result.exit_code == 0
    # Extract the JSON block from output (Rich wraps it); parse leniently.
    start = result.output.find("{")
    payload = json.loads(result.output[start : result.output.rfind("}") + 1])
    assert payload["total"] == 3
    assert payload["passed"] == 3
    assert payload["failed"] == 0
    assert len(payload["probes"]) == 3


def test_json_output_one_fail(runner, one_fail_probes):
    result = runner.invoke(app, ["self-test", "--json"])
    assert result.exit_code == 1
    start = result.output.find("{")
    payload = json.loads(result.output[start : result.output.rfind("}") + 1])
    assert payload["passed"] == 2
    assert payload["failed"] == 1


def test_only_filter_matches_substring(runner, all_pass_probes):
    result = runner.invoke(app, ["self-test", "--only", "beta"])
    assert result.exit_code == 0
    assert "beta probe" in result.output
    assert "alpha probe" not in result.output
    assert "gamma probe" not in result.output


def test_only_filter_matches_nothing(runner, all_pass_probes):
    result = runner.invoke(app, ["self-test", "--only", "no-such-probe"])
    # Zero matches → zero probes → still exits 0 (vacuously true).
    assert result.exit_code == 0


def test_haus_only_probe_returns_na_on_cli_only_install(runner, monkeypatch, tmp_path):
    """Verify that the _haus_only wrapper short-circuits to n/a when no
    haus-tier artifacts exist on disk. Simulates the friend's fresh
    Mac that ran `brew install sanctum-cli + sanctum onboard --recipe
    family` without ever deploying cathedrals/proxyd/R2D2."""

    # Point _haus_tier_installed at a fresh-fake home with no haus markers.
    monkeypatch.setattr(
        st,
        "_haus_tier_installed",
        lambda: False,
    )

    inner_called = {"value": False}

    def expensive_probe() -> st.ProbeResult:
        inner_called["value"] = True
        return st.ProbeResult(False, "should never run on cli-only")

    fake_probe = st._haus_only("test-probe", expensive_probe)
    result = fake_probe()

    assert result.not_applicable is True
    assert result.passed is True  # n/a counts as passing for exit-code purposes
    assert inner_called["value"] is False, "inner probe must NOT run when n/a"
    assert "CLI-only" in result.reason


def test_probe_that_raises_is_caught_as_fail(runner, monkeypatch):
    """A probe that throws an exception should fail-but-not-crash."""

    def boom() -> st.ProbeResult:
        raise RuntimeError("boom")

    fake = [
        st.Probe("alpha probe", lambda: st.ProbeResult(True, "ok")),
        st.Probe("exploding probe", boom),
    ]
    monkeypatch.setattr(st, "PROBES", fake)
    result = runner.invoke(app, ["self-test"])
    assert result.exit_code == 1
    assert "exploding probe" in result.output
    assert "boom" in result.output


def test_probe_proxy_key_passes_when_provisioned(monkeypatch):
    """CLI-7: the proxy-key probe passes when a real key resolves."""
    from sanctum_cli.commands import council

    monkeypatch.setattr(council, "proxy_key_provisioned", lambda: True)
    res = st.probe_proxy_key()
    assert res.passed is True
    assert "provisioned" in res.detail


def test_probe_proxy_key_fails_with_fallback_detail_when_unprovisioned(monkeypatch):
    """CLI-7: when the key is unresolved the probe FAILS with a FALLBACK row —
    making the silent-auth failure visible before proxyd enforces."""
    from sanctum_cli.commands import council

    monkeypatch.setattr(council, "proxy_key_provisioned", lambda: False)
    res = st.probe_proxy_key()
    assert res.passed is False
    assert "FALLBACK" in res.detail


def test_proxy_key_probe_is_registered_in_the_fleet():
    """The probe is wired into PROBES so self-test actually runs it."""
    assert any("proxy key" in p.name.lower() for p in st.PROBES)
