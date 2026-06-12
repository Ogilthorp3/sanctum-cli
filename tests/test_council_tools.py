"""Council tool registry — redaction, audit ledger, registry, mutate gate.

The council's own constraints (fan-out ruling 2026-06-12): every tool
output is redacted before the model sees it, every call is audited
before its result returns, the read/mutate split is enforced below the
model, and no mutation runs without a human y/N at a real REPL.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sanctum_cli.commands import council_tools as ct

if TYPE_CHECKING:
    from pathlib import Path


class TestRedact:
    def test_hostile_secrets_are_scrubbed(self) -> None:
        # hostile per Contracts-at-the-Boundary rule 4: a real-shaped key,
        # a PEM block, and a literal % that must survive untouched
        hostile = (
            "ok line with 100% normal text\n"
            "x-api-key: sk-ant-api03-" + "A" * 50 + "\n"
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
        )
        out = ct.redact(hostile)
        assert "sk-ant-" not in out
        assert "BEGIN OPENSSH PRIVATE KEY" not in out
        assert "100% normal text" in out, "redaction must not mangle innocent text"
        assert "[REDACTED:" in out
        assert "abc" not in out, "the key BODY must die, not just the BEGIN header"
        assert "END OPENSSH PRIVATE KEY" not in out

    def test_clean_text_passes_unchanged(self) -> None:
        clean = "agents: 12 running, 0 failed — disk 77%"
        assert ct.redact(clean) == clean


class TestAuditLedger:
    def test_every_call_appends_one_parseable_line(self, tmp_path: Path) -> None:
        ledger = tmp_path / "audit.jsonl"
        ct.audit(
            ledger,
            seat="Yoda",
            session="repl-test",
            tool="sanctum_status",
            params={"probe": "disk"},
            kind="read",
            mode="auto",
            outcome="ok",
            duration_ms=12,
        )
        ct.audit(
            ledger,
            seat="Yoda",
            session="repl-test",
            tool="logs_tail",
            params={"service": "vault"},
            kind="read",
            mode="auto",
            outcome="error",
            duration_ms=3,
        )
        lines = ledger.read_text().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["tool"] == "sanctum_status"
        assert first["outcome"] == "ok"
        assert "ts" in first

    def test_ledger_dir_is_created(self, tmp_path: Path) -> None:
        ledger = tmp_path / "deep" / "nested" / "audit.jsonl"
        ct.audit(
            ledger,
            seat="Mon Mothma",
            session="s",
            tool="agent_list",
            params={},
            kind="read",
            mode="auto",
            outcome="ok",
            duration_ms=1,
        )
        assert ledger.exists()


class TestRegistry:
    def test_four_read_tools_registered_no_mutations(self) -> None:
        assert set(ct.REGISTRY) == {"sanctum_status", "sanctum_doctor", "agent_list", "logs_tail"}
        assert all(t.kind == "read" for t in ct.REGISTRY.values()), "phase 1 ships reads only"

    def test_schemas_are_anthropic_shaped(self) -> None:
        for tool in ct.REGISTRY.values():
            assert tool.input_schema["type"] == "object"
            assert isinstance(tool.description, str) and tool.description

    def test_run_tool_unknown_name_is_error_not_crash(self, tmp_path: Path) -> None:
        result = ct.run_tool(
            "rm_dash_rf", {}, seat="Yoda", session="s", ledger=tmp_path / "a.jsonl"
        )
        assert result.is_error
        assert "unknown tool" in result.content.lower()
        # even the refusal is audited
        assert (tmp_path / "a.jsonl").exists()

    def test_run_tool_executes_audits_and_redacts(self, tmp_path: Path) -> None:
        # swap in a stub tool so the test owns the output; the real tools
        # are exercised by their own smoke below
        stub = ct.CouncilTool(
            name="stub",
            description="test stub",
            input_schema={"type": "object", "properties": {}},
            kind="read",
            run=lambda params: "key sk-ant-api03-" + "B" * 50,
        )
        result = ct.run_tool(
            "stub",
            {},
            seat="Yoda",
            session="s",
            ledger=tmp_path / "a.jsonl",
            registry={"stub": stub},
        )
        assert not result.is_error
        assert "sk-ant-" not in result.content, "redaction wraps every executor"
        line = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
        assert line["tool"] == "stub" and line["outcome"] == "ok"

    def test_executor_exception_becomes_error_result(self, tmp_path: Path) -> None:
        def boom(params: dict[str, object]) -> str:
            raise RuntimeError("doctor exploded")

        stub = ct.CouncilTool(
            name="boom", description="d", input_schema={"type": "object"}, kind="read", run=boom
        )
        result = ct.run_tool(
            "boom",
            {},
            seat="Yoda",
            session="s",
            ledger=tmp_path / "a.jsonl",
            registry={"boom": stub},
        )
        assert result.is_error and "doctor exploded" in result.content
        line = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
        assert line["outcome"] == "error"


class TestRealReadTools:
    def test_agent_list_runs_against_this_mac(self, tmp_path: Path) -> None:
        # real executor, real launchctl — this box runs com.sanctum agents
        result = ct.run_tool(
            "agent_list", {}, seat="Yoda", session="s", ledger=tmp_path / "a.jsonl"
        )
        assert not result.is_error
        assert "com.sanctum." in result.content

    def test_logs_tail_caps_lines_and_handles_unknown_service(self, tmp_path: Path) -> None:
        bad = ct.run_tool(
            "logs_tail",
            {"service": "no-such-svc-xyz"},
            seat="Yoda",
            session="s",
            ledger=tmp_path / "a.jsonl",
        )
        assert bad.is_error
        cap = ct.REGISTRY["logs_tail"].input_schema["properties"]["lines"]
        assert cap.get("maximum") == 200, "the cap is part of the published schema"
