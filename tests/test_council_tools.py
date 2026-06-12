"""Council tool registry — redaction, audit ledger, registry, mutate gate.

The council's own constraints (fan-out ruling 2026-06-12): every tool
output is redacted before the model sees it, every call is audited
before its result returns, the read/mutate split is enforced below the
model, and no mutation runs without a human y/N at a real REPL.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

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

    def test_kebab_word_containing_sk_not_redacted(self) -> None:
        # Fix 5 regression: kebab-case words like task-scheduler-... that
        # contain "sk-" followed by 40+ chars must NOT be flagged as keys.
        safe = "task-scheduler-watchdog-relaunch-attempt-counter-overflow detected"
        assert ct.redact(safe) == safe, (
            "kebab-case internal sk- substring must pass redact() unchanged"
        )

    def test_real_openai_shaped_key_at_line_start_is_redacted(self) -> None:
        # A real-shaped sk- 48-char key at line start must still be caught.
        key_line = "sk-" + "A" * 48 + "\nsome other line"
        out = ct.redact(key_line)
        assert "sk-" + "A" * 48 not in out, "bare sk- key must be redacted"
        assert "[REDACTED:" in out


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

    def test_dispatch_layer_mutate_gate_dormant(self, tmp_path: Path) -> None:
        # register a stub mutate tool; run_tool must refuse it structurally
        def no_op(params: dict[str, object]) -> str:
            return "should not run"

        stub_mutate = ct.CouncilTool(
            name="mutate_stub",
            description="d",
            input_schema={"type": "object"},
            kind="mutate",
            run=no_op,
        )
        result = ct.run_tool(
            "mutate_stub",
            {},
            seat="Yoda",
            session="s",
            ledger=tmp_path / "a.jsonl",
            registry={"mutate_stub": stub_mutate},
        )
        # must fail with the dormancy message
        assert result.is_error
        assert "not wired in phase 1" in result.content
        # must be audited with outcome error
        line = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
        assert line["outcome"] == "error"
        assert line["tool"] == "mutate_stub"
        assert line["kind"] == "mutate"


class TestLogsRuntimeClamp:
    """Fix 2a — the CODE clamp runs at runtime, not just in the schema."""

    def test_10000_lines_param_yields_at_most_200_content_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Write a tmp log with 300 lines of distinct content.
        log_file = tmp_path / "svc.log"
        log_file.write_text("\n".join(f"line-{i:04d}" for i in range(300)) + "\n", encoding="utf-8")

        # Monkeypatch LOG_MAP so the executor picks up our tmp file.
        import sanctum_cli.commands.logs as logs_mod

        monkeypatch.setattr(logs_mod, "LOG_MAP", {"testsvc": [log_file]})

        result = ct.run_tool(
            "logs_tail",
            {"service": "testsvc", "lines": 10000},
            seat="Yoda",
            session="s",
            ledger=tmp_path / "a.jsonl",
        )
        assert not result.is_error, result.content

        # Count actual content lines (exclude header and truncation notice lines).
        content_lines = [
            ln
            for ln in result.content.splitlines()
            if not ln.startswith("──") and not ln.startswith("[truncated:")
        ]
        assert len(content_lines) <= 200, (
            f"runtime clamp must enforce ≤200 lines; got {len(content_lines)}"
        )
        # The NEWEST line (line-0299) must survive.
        assert "line-0299" in result.content, "newest line must survive the clamp"


class TestLogsBytebudget:
    """Fix 1 — 24KB byte budget: oversized output is truncated with a marker."""

    def test_300_fat_lines_fit_within_byte_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_file = tmp_path / "fat.log"
        # 300 lines x 500 chars each = 150KB raw; must be cut to <=24KB.
        log_file.write_text("\n".join("X" * 500 for _ in range(300)) + "\n", encoding="utf-8")

        import sanctum_cli.commands.logs as logs_mod

        monkeypatch.setattr(logs_mod, "LOG_MAP", {"fatsvc": [log_file]})

        result = ct.run_tool(
            "logs_tail",
            {"service": "fatsvc", "lines": 200},
            seat="Yoda",
            session="s",
            ledger=tmp_path / "a.jsonl",
        )
        assert not result.is_error, result.content

        byte_len = len(result.content.encode("utf-8", errors="replace"))
        slack = 256
        assert byte_len <= ct.LOGS_TAIL_MAX_BYTES + slack, (
            f"output {byte_len} bytes exceeds budget {ct.LOGS_TAIL_MAX_BYTES} + {slack} slack"
        )
        assert "[truncated:" in result.content, "truncation marker must be present"
        # Newest line (last written) must survive.
        # The last written line is 500 X's; it must appear in the output.
        assert "X" * 500 in result.content, "newest (last) line must survive truncation"


class TestStatusNoRestic:
    """Fix 3 — sanctum_status must NOT invoke restic probes."""

    def test_status_does_not_call_restic_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Monkeypatch _restic_check to raise so any call poisons the result.
        from sanctum_cli.commands import doctor

        def restic_must_not_run(repo: str, password: str) -> doctor.RepoRow:
            raise AssertionError("restic must not run for status")

        monkeypatch.setattr(doctor, "_restic_check", restic_must_not_run)

        # Point SANCTUM_INSTANCE_FILE at a valid minimal config.
        inst = tmp_path / "instance.yaml"
        inst.write_text("instance:\n  name: Test\n  slug: test\n", encoding="utf-8")
        monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

        result = ct.run_tool(
            "sanctum_status", {}, seat="Yoda", session="s", ledger=tmp_path / "a.jsonl"
        )
        # The tool must not have crashed from the AssertionError.
        assert not result.is_error, (
            f"sanctum_status must succeed without restic; got: {result.content}"
        )

    def test_status_with_missing_config_returns_error_result_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point SANCTUM_INSTANCE_FILE at a non-existent path.
        monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "missing.yaml"))

        result = ct.run_tool(
            "sanctum_status", {}, seat="Yoda", session="s", ledger=tmp_path / "a.jsonl"
        )
        # Must return an error result (or a config-error string), never an exception.
        # Either is_error=True or the content mentions config/missing.
        mentions_config = "config" in result.content.lower() or "missing" in result.content.lower()
        assert result.is_error or mentions_config, (
            f"missing config must surface gracefully; got: {result.content!r}"
        )


class TestMutateGateDormant:
    def test_no_mutate_tool_ships_in_phase_one(self) -> None:
        assert not [t for t in ct.REGISTRY.values() if t.kind == "mutate"]

    def test_mount_excludes_mutations_without_tty(self) -> None:
        stub_read = ct.CouncilTool(
            name="r",
            description="d",
            input_schema={"type": "object"},
            kind="read",
            run=lambda p: "",
        )
        stub_mut = ct.CouncilTool(
            name="m",
            description="d",
            input_schema={"type": "object"},
            kind="mutate",
            run=lambda p: "",
        )
        reg = {"r": stub_read, "m": stub_mut}
        assert {t.name for t in ct.mount_tools(("r", "m"), registry=reg, is_tty=True)} == {
            "r",
            "m",
        }
        assert {t.name for t in ct.mount_tools(("r", "m"), registry=reg, is_tty=False)} == {"r"}, (
            "fail closed: no human at the REPL, no mutations mounted"
        )

    def test_confirm_requires_explicit_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answers = iter(["", "n", "yes", "y", "Y"])
        monkeypatch.setattr(ct, "_read_confirmation", lambda prompt: next(answers))
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is False  # Enter
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is False  # n
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is False  # 'yes' != y
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is True  # y
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is True  # Y

    def test_ctrl_c_and_eof_decline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def interrupt(prompt: str) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(ct, "_read_confirmation", interrupt)
        assert ct.confirm_mutation("restart com.sanctum.vault") is False

        def eof(prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr(ct, "_read_confirmation", eof)
        assert ct.confirm_mutation("restart com.sanctum.vault") is False


class TestRealReadTools:
    @pytest.mark.skipif(
        not any(
            b"com.sanctum." in line
            for line in subprocess.run(
                ["launchctl", "list"], capture_output=True
            ).stdout.splitlines()
        ),
        reason="needs a live sanctum host with com.sanctum.* agents loaded",
    )
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
