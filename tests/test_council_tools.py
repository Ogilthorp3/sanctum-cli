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
