"""``sanctum upgrade`` — toolchain currency contract.

Drives check/apply through Typer's ``CliRunner`` with a scripted runner
seam (recorded brew/npm/pip outputs) — no real package manager is ever
touched. The contract the command MUST honor:

* check is READ-ONLY and exits 1 when upgrades exist, 0 when current.
* latest stable only: npm resolves the ``latest`` dist-tag, never beta.
* ``brew pin`` → HOLD: a pinned formula is reported but never upgraded.
* npm install-aliases (``denchclaw@npm:openclaw``) are recognized and
  upgraded through the alias, not as a second package.
* apply re-probes the version afterwards (honest-verify): a tool whose
  version did not change reports FAILED, not ok.
* absent tools are skipped silently — one registry serves every machine.
"""

from __future__ import annotations

import json

import pytest
import typer

from sanctum_cli.commands import upgrade as up

BREW_LIST = "node 26.5.0\ntailscale 1.86.0\nsanctum-cli 0.10.0\n"
BREW_OUTDATED = json.dumps(
    {
        "formulae": [
            {
                "name": "node",
                "installed_versions": ["26.5.0"],
                "current_version": "26.6.1",
            }
        ],
        "casks": [],
    }
)
NPM_LS = json.dumps(
    {
        "dependencies": {
            "denchclaw": {"version": "2026.4.14"},
            "agent-browser": {"version": "0.27.1"},
        }
    }
)


class ScriptedRunner:
    """Runner seam returning recorded outputs; records every invocation."""

    def __init__(self, script: dict[str, up.RunResult], pinned: str = ""):
        self.script = script
        self.pinned = pinned
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: int = 0) -> up.RunResult:
        self.calls.append(list(cmd))
        key = " ".join(cmd)
        if key == "brew list --pinned":
            return up.RunResult(0, self.pinned, "")
        # Longest matching prefix wins, so a specific entry ("brew list
        # --versions node") shadows a broad fixture ("brew list --versions").
        best = max((p for p in self.script if key.startswith(p)), key=len, default=None)
        if best is not None:
            return self.script[best]
        return up.RunResult(0, "", "")


@pytest.fixture
def scripted() -> ScriptedRunner:
    return ScriptedRunner(
        {
            "brew list --versions": up.RunResult(0, BREW_LIST, ""),
            "brew outdated": up.RunResult(0, BREW_OUTDATED, ""),
            "npm ls -g": up.RunResult(0, NPM_LS, ""),
            "npm view openclaw": up.RunResult(0, "2026.7.1-2\n", ""),
            "npm view agent-browser": up.RunResult(0, "0.27.1\n", ""),
        }
    )


def plan_by_name(rows: list[up.PlanRow]) -> dict[str, up.PlanRow]:
    return {r.spec.name: r for r in rows}


class TestPlan:
    def test_outdated_brew_formula_is_an_upgrade_row(self, scripted: ScriptedRunner) -> None:
        rows = plan_by_name(up.build_plan(scripted))
        assert rows["node"].state == "upgrade"
        assert rows["node"].installed == "26.5.0"
        assert rows["node"].latest == "26.6.1"

    def test_current_brew_formula_is_ok(self, scripted: ScriptedRunner) -> None:
        rows = plan_by_name(up.build_plan(scripted))
        assert rows["tailscale"].state == "ok"
        assert rows["tailscale"].installed == rows["tailscale"].latest == "1.86.0"

    def test_pinned_formula_holds(self, scripted: ScriptedRunner) -> None:
        scripted.pinned = "node\n"
        rows = plan_by_name(up.build_plan(scripted))
        assert rows["node"].state == "hold"
        assert rows["node"].note == "brew pin"

    def test_npm_alias_resolves_to_canonical_package(self, scripted: ScriptedRunner) -> None:
        rows = plan_by_name(up.build_plan(scripted))
        assert rows["openclaw"].state == "upgrade"
        assert rows["openclaw"].installed == "2026.4.14"
        assert rows["openclaw"].latest == "2026.7.1-2"
        assert rows["openclaw"].note == "installed as denchclaw"

    def test_npm_latest_queries_dist_tag_latest_never_beta(self, scripted: ScriptedRunner) -> None:
        up.build_plan(scripted)
        views = [c for c in scripted.calls if c[:2] == ["npm", "view"]]
        assert views, "npm view must be consulted for latest stable"
        assert all(c[-1] == "dist-tags.latest" for c in views)

    def test_absent_tools_are_absent_not_errors(self, scripted: ScriptedRunner) -> None:
        rows = plan_by_name(up.build_plan(scripted))
        assert rows["restic"].state == "absent"
        assert rows["mlx-lm"].state == "absent"

    def test_check_is_read_only(self, scripted: ScriptedRunner) -> None:
        up.build_plan(scripted)
        mutating = [
            c for c in scripted.calls if "upgrade" in c or "install" in c[1:2] or "--upgrade" in c
        ]
        assert mutating == []


class TestApply:
    def test_npm_alias_upgrade_installs_through_the_alias(self) -> None:
        spec = next(s for s in up.REGISTRY if s.name == "openclaw")
        row = up.PlanRow(spec, "upgrade", "2026.4.14", "2026.7.1-2", "installed as denchclaw")
        assert up.upgrade_command_for(row) == [
            "npm",
            "install",
            "-g",
            "denchclaw@npm:openclaw@2026.7.1-2",
        ]

    def test_brew_upgrade_command_targets_the_formula(self) -> None:
        spec = next(s for s in up.REGISTRY if s.name == "node")
        row = up.PlanRow(spec, "upgrade", "26.5.0", "26.6.1")
        assert up.upgrade_command_for(row) == ["brew", "upgrade", "node"]

    def test_unchanged_version_after_upgrade_is_failure(self, scripted: ScriptedRunner) -> None:
        spec = next(s for s in up.REGISTRY if s.name == "node")
        row = up.PlanRow(spec, "upgrade", "26.5.0", "26.6.1")
        scripted.script["brew upgrade node"] = up.RunResult(0, "", "")
        scripted.script["brew list --versions node"] = up.RunResult(0, "node 26.5.0\n", "")
        res = up.apply_row(scripted, row)
        assert not res.ok
        assert "unchanged" in res.detail

    def test_successful_upgrade_reprobes_and_reports_new_version(
        self, scripted: ScriptedRunner
    ) -> None:
        spec = next(s for s in up.REGISTRY if s.name == "node")
        row = up.PlanRow(spec, "upgrade", "26.5.0", "26.6.1")
        scripted.script["brew upgrade node"] = up.RunResult(0, "", "")
        scripted.script["brew list --versions node"] = up.RunResult(0, "node 26.6.1\n", "")
        scripted.script["node --version"] = up.RunResult(0, "v26.6.1\n", "")
        res = up.apply_row(scripted, row)
        assert res.ok
        assert res.now == "26.6.1"

    def test_failed_post_check_is_failure(self, scripted: ScriptedRunner) -> None:
        spec = next(s for s in up.REGISTRY if s.name == "node")
        row = up.PlanRow(spec, "upgrade", "26.5.0", "26.6.1")
        scripted.script["brew upgrade node"] = up.RunResult(0, "", "")
        scripted.script["brew list --versions node"] = up.RunResult(0, "node 26.6.1\n", "")
        scripted.script["node --version"] = up.RunResult(1, "", "boom")
        res = up.apply_row(scripted, row)
        assert not res.ok
        assert "post-check" in res.detail


class TestCommand:
    def test_exit_1_when_upgrades_available(self, scripted: ScriptedRunner) -> None:
        with pytest.raises(typer.Exit) as exc:
            up.upgrade_command(run=scripted)
        assert exc.value.exit_code == 1

    def test_exit_0_when_toolchain_current(self, scripted: ScriptedRunner) -> None:
        scripted.script["brew outdated"] = up.RunResult(0, '{"formulae": [], "casks": []}', "")
        scripted.script["npm ls -g"] = up.RunResult(
            0, json.dumps({"dependencies": {"denchclaw": {"version": "2026.7.1-2"}}}), ""
        )
        with pytest.raises(typer.Exit) as exc:
            up.upgrade_command(run=scripted)
        assert exc.value.exit_code == 0

    def test_unknown_only_filter_exits_2(self, scripted: ScriptedRunner) -> None:
        with pytest.raises(typer.Exit) as exc:
            up.upgrade_command(only="no-such-tool", run=scripted)
        assert exc.value.exit_code == 2

    def test_json_check_emits_machine_readable_plan(
        self, scripted: ScriptedRunner, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(typer.Exit):
            up.upgrade_command(json_output=True, run=scripted)
        payload = json.loads(capsys.readouterr().out)
        by_tool = {r["tool"]: r for r in payload}
        assert by_tool["openclaw"]["state"] == "upgrade"
        assert by_tool["openclaw"]["latest"] == "2026.7.1-2"
