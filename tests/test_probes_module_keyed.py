"""Tests for Task 9 — module-keyed probe registry.

Part (a): regression guard — existing CLI+haus probes still run, ``--only``
         still filters, exit codes unchanged.

Part (b): a module that declares ``probes: [...]`` contributes those probes
         under its own module key in the keyed registry.

Part (c): ``run_soak`` populates ``red_probes`` from a failing module probe.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import self_test as st
from sanctum_cli.modules.manifest import ModuleManifest
from sanctum_cli.modules.registry import ModuleRegistry
from sanctum_cli.soak.harness import (
    run_soak,
)

# ── helpers ──────────────────────────────────────────────────────────────


def _make_manifest(name: str, probes: list[str] | None = None) -> ModuleManifest:
    return ModuleManifest.model_validate(
        {
            "module": name,
            "version": "1.0.0",
            "description": name,
            "probes": probes or [],
            "docs": "https://x.invalid",
            "demo": "true",
        }
    )


def _make_registry(*modules: tuple[str, list[str]]) -> ModuleRegistry:
    """Build a registry with named modules each carrying the given probe paths."""
    manifests = {name: _make_manifest(name, probes) for name, probes in modules}
    return ModuleRegistry(manifests=manifests)


# ── Part (a): regression guard — existing CLI+haus probes ────────────────


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def all_pass_probes(monkeypatch):
    """Patch PROBES to a controlled set (same interface as test_self_test.py)."""
    fake = [
        st.Probe("alpha probe", lambda: st.ProbeResult(True, "ok")),
        st.Probe("beta probe", lambda: st.ProbeResult(True, "ok")),
        st.Probe("gamma probe", lambda: st.ProbeResult(True, "ok")),
    ]
    monkeypatch.setattr(st, "PROBES", fake)
    return fake


def test_regression_all_pass_exits_zero(cli_runner, all_pass_probes):
    """Regression: monkeypatching PROBES still works — runner uses it."""
    result = cli_runner.invoke(app, ["self-test"])
    assert result.exit_code == 0


def test_regression_one_fail_exits_one(cli_runner, monkeypatch):
    """Regression: a failing probe in PROBES still produces exit code 1."""
    fake = [
        st.Probe("pass", lambda: st.ProbeResult(True, "ok")),
        st.Probe("fail", lambda: st.ProbeResult(False, "boom")),
    ]
    monkeypatch.setattr(st, "PROBES", fake)
    result = cli_runner.invoke(app, ["self-test"])
    assert result.exit_code == 1


def test_regression_only_filter_still_filters(cli_runner, all_pass_probes):
    """Regression: --only substring filter works after refactor."""
    result = cli_runner.invoke(app, ["self-test", "--only", "beta"])
    assert result.exit_code == 0
    assert "beta probe" in result.output
    assert "alpha probe" not in result.output
    assert "gamma probe" not in result.output


def test_regression_json_output_shape(cli_runner, all_pass_probes):
    """Regression: --json still produces the expected payload shape."""
    result = cli_runner.invoke(app, ["self-test", "--json"])
    assert result.exit_code == 0
    start = result.output.find("{")
    payload = json.loads(result.output[start : result.output.rfind("}") + 1])
    assert payload["total"] == 3
    assert payload["passed"] == 3
    assert payload["failed"] == 0


# ── Part (b): module_probes() contributes under the module key ───────────


def test_module_probes_returns_probes_for_module():
    """module_probes(registry) returns probes keyed by module name."""
    reg = _make_registry(("backup", ["backup.backup_recent"]))
    keyed = st.module_probes(reg)
    assert "backup" in keyed
    assert len(keyed["backup"]) == 1
    assert keyed["backup"][0].name == "backup.backup_recent"


def test_module_probes_bad_path_surfaces_as_failing_probe():
    """A probe dotted-path that can't be imported becomes a failing probe (no crash)."""
    reg = _make_registry(("mymod", ["does.not.exist.probe_fn"]))
    keyed = st.module_probes(reg)
    assert "mymod" in keyed
    probe = keyed["mymod"][0]
    result = probe.check()
    # Must fail gracefully, not raise
    assert result.passed is False
    assert "import" in result.detail.lower() or "does.not.exist" in result.detail


def test_module_probes_backup_resolves_to_callable():
    """The backup manifest's probe path resolves to the real probe_backup_recent fn."""
    reg = ModuleRegistry.discover()
    keyed = st.module_probes(reg)
    assert "backup" in keyed
    probe = keyed["backup"][0]
    # The callable resolves — calling it should return a ProbeResult (not raise).
    result = probe.check()
    assert isinstance(result, st.ProbeResult)


def test_module_probes_empty_probes_list():
    """A module with no declared probes contributes an empty list under its key."""
    reg = _make_registry(("bare", []))
    keyed = st.module_probes(reg)
    assert "bare" in keyed
    assert keyed["bare"] == []


def test_module_probes_multiple_modules():
    """Multiple modules each appear under their own key."""
    reg = _make_registry(
        ("alpha", ["backup.backup_recent"]),
        ("beta", ["backup.probe_network"]),
    )
    keyed = st.module_probes(reg)
    assert "alpha" in keyed
    assert "beta" in keyed


# ── Part (c): run_soak populates red_probes from a failing probe ─────────


class _FakeManifest:
    """Minimal manifest stub for testing the soak runner."""

    def __init__(self, module: str, probes: list[str], result_path: str) -> None:
        self.module = module
        self.probes = probes
        self.services = []

        class _Soak:
            pass

        soak = _Soak()
        soak.result_path = result_path  # type: ignore[attr-defined]
        self.soak = soak


class _FakeRegistry:
    """Registry stub that returns a controlled manifest."""

    def __init__(self, manifest: _FakeManifest) -> None:
        self._manifest = manifest

    def get(self, name: str) -> _FakeManifest:
        return self._manifest


def test_run_soak_populates_red_probes_when_probe_fails(tmp_path, monkeypatch):
    """run_soak writes red_probes to the sample when a module probe returns False."""
    result_file = tmp_path / "soak.json"
    manifest = _FakeManifest(
        module="testmod",
        probes=["backup.backup_recent"],  # real path, result doesn't matter
        result_path=str(result_file),
    )
    reg = _FakeRegistry(manifest)

    # Patch module_probes to return a probe that always fails.
    always_fail = st.Probe(
        "testmod.always_fail",
        lambda: st.ProbeResult(False, "forced fail"),
    )
    monkeypatch.setattr(
        "sanctum_cli.soak.harness.module_probes",
        lambda registry: {"testmod": [always_fail]},
    )

    run_soak("testmod", reg, once=True)  # type: ignore[arg-type]

    data = json.loads(result_file.read_text())
    assert data["samples"], "expected at least one sample"
    sample = data["samples"][0]
    assert "testmod.always_fail" in sample["red_probes"], "failing probe must appear in red_probes"


def test_run_soak_red_probes_empty_when_all_probes_pass(tmp_path, monkeypatch):
    """run_soak leaves red_probes empty when all module probes pass."""
    result_file = tmp_path / "soak.json"
    manifest = _FakeManifest(
        module="testmod",
        probes=["backup.backup_recent"],
        result_path=str(result_file),
    )
    reg = _FakeRegistry(manifest)

    always_pass = st.Probe(
        "testmod.always_pass",
        lambda: st.ProbeResult(True, "ok"),
    )
    monkeypatch.setattr(
        "sanctum_cli.soak.harness.module_probes",
        lambda registry: {"testmod": [always_pass]},
    )

    run_soak("testmod", reg, once=True)  # type: ignore[arg-type]

    data = json.loads(result_file.read_text())
    sample = data["samples"][0]
    assert sample["red_probes"] == []


def test_run_soak_no_module_probes_does_not_crash(tmp_path, monkeypatch):
    """If module_probes returns an empty dict (no probes), run_soak still works."""
    result_file = tmp_path / "soak.json"
    manifest = _FakeManifest(
        module="bare",
        probes=[],
        result_path=str(result_file),
    )
    reg = _FakeRegistry(manifest)

    monkeypatch.setattr(
        "sanctum_cli.soak.harness.module_probes",
        lambda registry: {},
    )

    run_soak("bare", reg, once=True)  # type: ignore[arg-type]

    data = json.loads(result_file.read_text())
    sample = data["samples"][0]
    assert sample["red_probes"] == []
