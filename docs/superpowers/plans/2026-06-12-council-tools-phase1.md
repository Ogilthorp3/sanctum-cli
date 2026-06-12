# Council Tools Phase 1 + Canon Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bounded read-only tools for Yoda + Mon Mothma in `sanctum council`, the May-31 English movie-voice canon in the CLI persona, and the VM-side persona-drift/TOOLS.md fixes.

**Architecture:** New `sanctum_cli/commands/council_tools.py` holds a typed registry (frozen dataclass per tool, `kind: read|mutate` enforced below the model), `redact()` built on `sanctum_cli/secret_scanner.py`'s pattern list, an append-only JSONL audit ledger, and the dormant mutate-confirm gate. `council.py` gains the Anthropic tool-use loop (buffered, capped at 8 calls/turn, error breaker, degrade-to-chat on 4xx) used only by seats whose `Seat.tools` is non-empty; toolless seats keep the existing streaming path untouched. Personas are composed by a `_persona(seat)` helper that appends an honest tool clause.

**Tech Stack:** Python 3.12 (`.venv/`), httpx, rich, pytest, ruff, mypy --strict. Repo: `~/Projects/sanctum-cli`, run tools as `.venv/bin/...` from repo root.

**Spec:** `docs/superpowers/specs/2026-06-12-council-tools-and-canon-voice-design.md`

**Phase-0 result (already run, 2026-06-12):** the `:3456` claude-max bridge does NOT forward `tools` (it wraps one-shot `claude --print`); proxyd itself translates tools fine (verified `tool_use` from qwen36-plus, glm-51, gemini-25-flash). Bert chose to fix the bridge in a separate project. **Consequence for this plan:** the tool loop is built and tested against a faked transport; the interactive live smoke is DEFERRED to the bridge plan. Everything else ships now.

**House rules binding every task:** gates before each commit — `.venv/bin/ruff format <touched>`, `.venv/bin/ruff check sanctum_cli tests`, `.venv/bin/mypy sanctum_cli`, `.venv/bin/python -m pytest -q`. Comments are full sentences explaining why. Never touch files outside your task's list.

---

### Task 1: `redact()` + audit ledger (foundations of `council_tools.py`)

**Files:**
- Create: `sanctum_cli/commands/council_tools.py`
- Create: `tests/test_council_tools.py`

- [ ] **Step 1: Read the pattern source.** Open `sanctum_cli/secret_scanner.py` and note the canonical pattern list (name → compiled `bytes` regex, e.g. `("anthropic-api-key", re.compile(rb"sk-ant-..."))`) and its exact variable name. The redactor reuses THAT list — do not copy patterns.

- [ ] **Step 2: Write the failing tests.** Create `tests/test_council_tools.py`:

```python
"""Council tool registry — redaction, audit ledger, registry, mutate gate.

The council's own constraints (fan-out ruling 2026-06-12): every tool
output is redacted before the model sees it, every call is audited
before its result returns, the read/mutate split is enforced below the
model, and no mutation runs without a human y/N at a real REPL.
"""

from __future__ import annotations

import json
from pathlib import Path

from sanctum_cli.commands import council_tools as ct


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
```

- [ ] **Step 3: Run to verify failure.** `.venv/bin/python -m pytest tests/test_council_tools.py -v` → collection error (module missing).

- [ ] **Step 4: Implement.** Create `sanctum_cli/commands/council_tools.py`:

```python
"""Bounded typed tools for the council chamber.

The registry IS the contract: each tool wraps sanctum's own internals
behind a JSON schema, is classified read-or-mutate at registration, has
its output redacted before any model sees it, and writes an append-only
audit line before its result is returned. No free-form shell tool
exists, in any phase — the council's own unanimous ruling (fan-out,
2026-06-12). Phase 1 registers reads only; the mutate gate ships
dormant and tested, waiting for phase 2's verbs.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sanctum_cli.secret_scanner import <PATTERN_LIST_NAME_FROM_STEP_1>

AUDIT_LEDGER = Path.home() / ".sanctum" / "logs" / "council-tools-audit.jsonl"


def redact(text: str) -> str:
    """Scrub known secret shapes before any model reads tool output.

    Reuses the backup gate's pattern list — one source of truth for what
    a secret looks like. Read-only is not safe-to-surface (council #9).
    """
    data = text.encode("utf-8", errors="replace")
    for name, pattern in <PATTERN_LIST_NAME_FROM_STEP_1>:
        data = pattern.sub(f"[REDACTED:{name}]".encode(), data)
    return data.decode("utf-8", errors="replace")


def audit(
    ledger: Path,
    *,
    seat: str,
    session: str,
    tool: str,
    params: dict[str, object],
    kind: str,
    mode: str,
    outcome: str,
    duration_ms: int,
) -> None:
    """Append one audit line; the ledger is the durable record of every
    call, read and mutate, success and failure (council #5). Params are
    logged, outputs are not — the ledger must not leak what redaction
    might miss."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seat": seat,
        "session": session,
        "tool": tool,
        "params": params,
        "kind": kind,
        "mode": mode,
        "outcome": outcome,
        "duration_ms": duration_ms,
    }
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
```

Replace `<PATTERN_LIST_NAME_FROM_STEP_1>` with the actual name found in Step 1 (it is a module-level list of `(name, compiled_bytes_pattern)` tuples; if the scanner's structure differs, adapt the loop but keep the single-source-of-truth import — copying patterns is a plan violation). If a pattern's regex is anchored in ways that don't fit free text, prefer fixing the call (e.g. `re.search` semantics via `pattern.sub`) over duplicating the pattern.

- [ ] **Step 5: Run tests.** `.venv/bin/python -m pytest tests/test_council_tools.py -v` → all pass.

- [ ] **Step 6: Gates + commit.**

```bash
.venv/bin/ruff format sanctum_cli/commands/council_tools.py tests/test_council_tools.py
.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q
git add sanctum_cli/commands/council_tools.py tests/test_council_tools.py
git commit -m "feat(council-tools): redaction on the read path + append-only audit ledger

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Tool registry + the four read tools

**Files:**
- Modify: `sanctum_cli/commands/council_tools.py`
- Modify: `tests/test_council_tools.py`

- [ ] **Step 1: Read the wrapped internals.** Before coding, read these (they exist; names verified):
  - `sanctum_cli/commands/doctor.py` — `collect(cfg) -> Report` (line ~308), `render_brief(report) -> str` (line ~243), the `Report`/`AgentRow`/`ProviderRow`/`RepoRow` dataclasses, and how `doctor_command` loads config.
  - `sanctum_cli/commands/agent.py` — `_launchctl_list() -> list[AgentRow]` and the plist log-path resolution used by its logs handling.
  - `sanctum_cli/commands/logs.py` — `logs_command` internals for how a service name resolves to a log file.

- [ ] **Step 2: Write the failing tests.** Append to `tests/test_council_tools.py`:

```python
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
            "boom", {}, seat="Yoda", session="s",
            ledger=tmp_path / "a.jsonl", registry={"boom": stub},
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
            "logs_tail", {"service": "no-such-svc-xyz"},
            seat="Yoda", session="s", ledger=tmp_path / "a.jsonl",
        )
        assert bad.is_error
        over = ct.REGISTRY["logs_tail"].input_schema["properties"]["lines"]
        assert over.get("maximum") == 200, "the cap is part of the published schema"
```

- [ ] **Step 3: Run to verify failure.** `.venv/bin/python -m pytest tests/test_council_tools.py -v` → new tests fail (`CouncilTool`, `REGISTRY`, `run_tool` missing).

- [ ] **Step 4: Implement in `council_tools.py`.** Add:

```python
@dataclass(frozen=True)
class CouncilTool:
    """One bounded instrument: schema in, text out, classified at birth."""

    name: str
    description: str
    input_schema: dict[str, object]
    kind: Literal["read", "mutate"]
    run: Callable[[dict[str, object]], str]


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
```

and `run_tool(name, params, *, seat, session, ledger=AUDIT_LEDGER, registry=None) -> ToolResult`:
  - unknown name → audit an `outcome="error"` line, return `ToolResult("unknown tool: <name>; known: <sorted names>", is_error=True)`
  - known → time the executor, catch `Exception` into an error result; audit BEFORE returning (ok or error); `redact()` the successful content.

Then the four executors, wrapping the internals read in Step 1 (no subprocess, no shell):
  - `sanctum_status` — `doctor.collect(config.load())` → `doctor.render_brief(report)` (the brief one-line health snapshot). Schema: `{"type": "object", "properties": {}}`.
  - `sanctum_doctor` — same `collect`, then a plain-text rendering of the full Report rows (agents, providers, repos with their statuses — build the text from the dataclasses; do NOT call `render_full`, which prints to a console). Schema: `{}` properties.
  - `agent_list` — `agent._launchctl_list()` rows formatted one per line: `label pid last_exit status`. Schema: `{}` properties.
  - `logs_tail` — `{"service": {"type": "string"}, "lines": {"type": "integer", "maximum": 200, "default": 50}}`, `required: ["service"]`. Resolve the service's log path exactly the way `logs.py`/`agent.py` already do (reuse their helper; if it's private, import the private name — same package). Unknown service → raise `ValueError` listing known services (run_tool converts to error result). Clamp `lines` to 200 server-side too (schema maxima are advisory to models).
  Register: `REGISTRY: dict[str, CouncilTool] = {t.name: t for t in (...)}`.

- [ ] **Step 5: Run tests.** All `test_council_tools.py` pass; full suite green.

- [ ] **Step 6: Gates + commit.**

```bash
.venv/bin/ruff format sanctum_cli/commands/council_tools.py tests/test_council_tools.py
.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q
git add sanctum_cli/commands/council_tools.py tests/test_council_tools.py
git commit -m "feat(council-tools): typed registry + four read instruments

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Mutate gate — built, tested, dormant

**Files:**
- Modify: `sanctum_cli/commands/council_tools.py`
- Modify: `tests/test_council_tools.py`

- [ ] **Step 1: Write the failing tests.** Append:

```python
class TestMutateGateDormant:
    def test_no_mutate_tool_ships_in_phase_one(self) -> None:
        assert not [t for t in ct.REGISTRY.values() if t.kind == "mutate"]

    def test_mount_excludes_mutations_without_tty(self) -> None:
        stub_read = ct.CouncilTool("r", "d", {"type": "object"}, "read", lambda p: "")
        stub_mut = ct.CouncilTool("m", "d", {"type": "object"}, "mutate", lambda p: "")
        reg = {"r": stub_read, "m": stub_mut}
        assert set(ct.mount_tools(("r", "m"), registry=reg, is_tty=True)) == {"r", "m"}
        assert set(ct.mount_tools(("r", "m"), registry=reg, is_tty=False)) == {"r"}, (
            "fail closed: no human at the REPL, no mutations mounted"
        )

    def test_confirm_requires_explicit_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answers = iter(["", "n", "yes", "y", "Y"])
        monkeypatch.setattr(
            ct, "_read_confirmation", lambda prompt: next(answers)
        )
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is False  # Enter
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is False  # n
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is False  # 'yes' != y
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is True   # y
        assert ct.confirm_mutation("restart LaunchAgent com.sanctum.vault") is True   # Y
```

Add `import pytest` to the test file imports if not already present.

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement**:

```python
def mount_tools(
    allowed: tuple[str, ...],
    *,
    registry: dict[str, CouncilTool] | None = None,
    is_tty: bool,
) -> list[CouncilTool]:
    """The seat's allowlist intersected with what this session may hold.

    Mutations are simply not mounted when no human is at the REPL —
    a confirm gate with no one to confirm fails closed (council #8).
    """
    reg = REGISTRY if registry is None else registry
    tools = [reg[name] for name in allowed if name in reg]
    if not is_tty:
        tools = [t for t in tools if t.kind == "read"]
    return tools


def _read_confirmation(prompt: str) -> str:
    """Separated for tests; the REPL's stdin is the only authority."""
    return input(prompt)


def confirm_mutation(resolved_action: str) -> bool:
    """Show the RESOLVED action, not a paraphrase; only a literal y/Y
    proceeds. Enter is no. 'yes' is no — the gate is strict on purpose
    (council #3, #7)."""
    answer = _read_confirmation(f"⚠ mutation: {resolved_action} — proceed? [y/N] ")
    return answer.strip() in ("y", "Y")
```

- [ ] **Step 4: Tests pass; gates; commit** as `feat(council-tools): dormant mutate gate — fail-closed mounting + strict y/N`. (Same gate ritual and Co-Authored-By trailer as Task 1.)

---

### Task 4: The tool loop in `council.py`

**Files:**
- Modify: `sanctum_cli/commands/council.py`
- Modify: `tests/test_council.py`

- [ ] **Step 1: Read the current say-path.** `council.py` `_repl()` tail (the thinking-dots handoff shipped 2026-06-12: `stream = _stream(...)`, `console.status(thinking_markup(seat), ...)`, `first = next(stream, None)` …). Your loop ADDS a branch; the streaming path stays byte-identical for toolless seats. Also read `TestFanOut` in `tests/test_council.py` for the established monkeypatch-the-module-transport pattern.

- [ ] **Step 2: Write the failing tests.** Append to `tests/test_council.py`:

```python
class TestToolLoop:
    """The loop is tested against a faked transport (the bridge can't
    tool yet — phase-0 finding); the live smoke belongs to the bridge
    plan. The fake speaks the Anthropic protocol: content blocks +
    stop_reason."""

    @staticmethod
    def _fake_responses(*responses: dict) -> object:
        seq = iter(responses)

        def fake_post(seat, messages, *, system, tools):  # noqa: ANN001, ANN202
            return next(seq)

        return fake_post

    def test_tool_turn_executes_then_answers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        monkeypatch.setattr(
            cc,
            "_post_with_tools",
            self._fake_responses(
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {"type": "text", "text": "Check the agents, I must."},
                        {"type": "tool_use", "id": "tu_1", "name": "agent_list", "input": {}},
                    ],
                },
                {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "Healthy, the agents are."}],
                },
            ),
        )
        seat = cc.SEATS["yoda"]
        answer = cc._tool_turn(seat, [{"role": "user", "content": "agents ok?"}])
        assert answer == "Healthy, the agents are."
        audit_lines = (tmp_path / "a.jsonl").read_text().splitlines()
        assert len(audit_lines) == 1, "the agent_list call was audited"

    def test_cap_stops_runaway_turns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        endless = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "tu_n", "name": "agent_list", "input": {}}],
        }
        monkeypatch.setattr(cc, "_post_with_tools", self._fake_responses(*([endless] * 20)))
        answer = cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "loop!"}])
        assert len((tmp_path / "a.jsonl").read_text().splitlines()) <= cc.TOOL_CALL_CAP
        assert isinstance(answer, str), "a capped turn still returns an answer"

    def test_breaker_after_two_consecutive_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        bad_call = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "tu_e", "name": "nonexistent", "input": {}}],
        }
        final = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Hmm."}]}
        monkeypatch.setattr(
            cc, "_post_with_tools", self._fake_responses(bad_call, bad_call, final)
        )
        answer = cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "q"}])
        assert answer == "Hmm."

    def test_4xx_with_tools_degrades_to_chat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def reject(seat, messages, *, system, tools):  # noqa: ANN001, ANN202
            raise cc.ToolsRejected("HTTP 400: unknown field tools")

        monkeypatch.setattr(cc, "_post_with_tools", reject)
        with pytest.raises(cc.ToolsRejected):
            cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "q"}])
        # the REPL catches ToolsRejected and falls back to the streaming
        # path — asserted structurally: the except clause exists
        import inspect

        src = inspect.getsource(cc._repl)
        assert "ToolsRejected" in src


class TestSeatTools:
    def test_yoda_and_mothma_are_armed_others_are_not(self) -> None:
        armed = {k for k, s in cc.SEATS.items() if s.tools}
        assert armed == {"yoda", "mothma"}
        for k in armed:
            assert set(cc.SEATS[k].tools) == {
                "sanctum_status", "sanctum_doctor", "agent_list", "logs_tail",
            }


class TestPersonaComposition:
    def test_armed_seats_get_instruments_clause(self) -> None:
        text = cc._persona(cc.SEATS["yoda"])
        assert "Instruments you have" in text
        assert "sanctum_status" in text

    def test_toolless_seats_keep_the_no_tools_truth(self) -> None:
        text = cc._persona(cc.SEATS["windu"])
        assert "NO tools" in text
```

Add `from pathlib import Path` and `from sanctum_cli.commands import council_tools` imports as needed (check the test file's existing imports first).

- [ ] **Step 3: Run to verify failure.** New tests fail (`_post_with_tools`, `_tool_turn`, `TOOL_CALL_CAP`, `ToolsRejected`, `_persona`, `Seat.tools` missing).

- [ ] **Step 4: Implement in `council.py`.**

  - `Seat` gains `tools: tuple[str, ...] = ()` (defaulted — only yoda/mothma set it):
    yoda + mothma: `tools=("sanctum_status", "sanctum_doctor", "agent_list", "logs_tail"),`
  - Module imports: `from sanctum_cli.commands import council_tools`.
  - `TOOL_CALL_CAP = 8` module constant; `class ToolsRejected(RuntimeError): ...`
  - `_post_with_tools(seat, messages, *, system, tools) -> dict` — buffered POST like `_complete` but payload includes `"tools": tools` and returns the full response dict; on 4xx raise `ToolsRejected(f"HTTP {status}: {body[:160]}")`; other failures raise as today.
  - `_persona(seat) -> str` — `seat.persona` + (instruments clause listing each mounted tool name + one-line description, plus: "Claims about live state must come from a tool result in this conversation, not from memory. A capability you lack, name the operator's command instead.") for armed seats; for unarmed seats append the existing "You are a chat seat with NO tools — never claim to have run a command, read a file, or observed live state." line — and REMOVE that sentence from Yoda's persona prose (it moves into the helper; see Task 5 which rewrites Yoda's persona anyway — coordinate: Task 4 moves the clause, Task 5 rewrites the prose).
  - `_tool_turn(seat, messages) -> str` — the loop:

```python
def _tool_turn(seat: Seat, messages: list[dict[str, object]]) -> str:
    """Buffered Anthropic tool-use loop for armed seats. The cap and the
    breaker keep a confused model from sawing at the instruments all
    night (council #6)."""
    mounted = council_tools.mount_tools(seat.tools, is_tty=console.is_terminal)
    specs = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in mounted
    ]
    convo: list[dict[str, object]] = list(messages)
    session = f"repl-{os.getpid()}"
    calls = 0
    consecutive_errors = 0
    while True:
        data = _post_with_tools(seat, convo, system=_persona(seat), tools=specs)
        blocks = data.get("content", [])
        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
        if data.get("stop_reason") != "tool_use" or not tool_uses:
            return "".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            ).strip()
        results: list[dict[str, object]] = []
        for block in tool_uses:
            calls += 1
            if calls > TOOL_CALL_CAP or consecutive_errors >= 2:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": "tool budget exhausted — answer with what you have",
                        "is_error": True,
                    }
                )
                continue
            with console.status(
                f"[{seat.style}]{seat.label} consults the instruments… ({block['name']})[/]",
                spinner="simpleDotsScrolling",
                spinner_style=seat.style,
            ):
                result = council_tools.run_tool(
                    str(block["name"]), dict(block.get("input") or {}),
                    seat=seat.label, session=session,
                )
            consecutive_errors = consecutive_errors + 1 if result.is_error else 0
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": result.content,
                    "is_error": result.is_error,
                }
            )
        convo.append({"role": "assistant", "content": blocks})
        convo.append({"role": "user", "content": results})
```

  (Add `import os` if absent. Note `run_tool`'s ledger default is `AUDIT_LEDGER` — the test monkeypatches the module attribute, so `run_tool` must read `council_tools.AUDIT_LEDGER` at CALL time, not bind it at def time: make the parameter `ledger: Path | None = None` resolving `None → AUDIT_LEDGER` inside the body. Adjust Task 1/2's signature accordingly if it was built with a def-time default — this is the one cross-task signature fix.)

  - REPL say-path branch: armed seats route through `_tool_turn`; `ToolsRejected` falls back to the existing streaming path with `console.print("[dim](seat's model declines tools — chat only this turn)[/]")`:

```python
        seat = SEATS[active]
        transcript.add("user", action.arg)
        if seat.tools:
            try:
                with console.status(
                    thinking_markup(seat),
                    spinner="simpleDotsScrolling",
                    spinner_style=seat.style,
                ):
                    answer = _tool_turn(seat, transcript.messages())
                console.print(f"[{seat.style}]{seat.label}:[/] {answer}")
                transcript.add("assistant", answer)
                continue
            except ToolsRejected:
                console.print("[dim](seat's model declines tools — chat only this turn)[/]")
            except Exception as e:
                console.print(f"[red]⚠ {e}[/]")
                transcript.add("assistant", "(seat unavailable)")
                continue
        # …existing streaming path unchanged below…
```

  Careful: the `_tool_turn` status wraps the WHOLE buffered loop, and the per-tool status nests inside it — rich does not support nested `console.status`. Fix: do NOT wrap `_tool_turn` in an outer status; instead `_tool_turn` itself opens the verb status (`thinking_markup`) around each `_post_with_tools` call and the instruments status around each `run_tool` — sequential, never nested. Implement it that way (the snippet above shows intent; restructure so statuses never nest).

- [ ] **Step 5: Tests pass; full suite; gates.** Also re-run the matrix/banner suites to prove no collateral.

- [ ] **Step 6: Commit** as `feat(council): Anthropic tool loop for armed seats — capped, audited, degrades to chat`. (Gate ritual + trailer as Task 1.)

---

### Task 5: Yoda's canon persona

**Files:**
- Modify: `sanctum_cli/commands/council.py` (the yoda `Seat` persona string only)
- Modify: `tests/test_council.py`

- [ ] **Step 1: Failing test.** Append:

```python
class TestCanonVoice:
    def test_yoda_persona_carries_the_may31_canon(self) -> None:
        p = cc.SEATS["yoda"].persona
        assert "invert" in p.lower(), "movie-voice inversion is the default register"
        assert "plain" in p.lower() and "tool" in p.lower(), (
            "the machine-boundary line is load-bearing now that he has tools"
        )
        assert "NO tools" not in p, "the tool clause is composed by _persona(), not hardcoded"
```

- [ ] **Step 2: Replace Yoda's persona** in `SEATS["yoda"]` with (provenance comment above it: `# Condensed from the May-31 canon: vm:~/.openclaw/workspace/IDENTITY.md`):

```python
        persona=(
            "You are Yoda, Grand Master of the Sanctum Jedi Council — the wise"
            " synthesist, and the Jedi Master himself, not an impression."
            " Sanctum is a self-hosted family AI and haus-ops platform on a Mac"
            " Mini ('manoir') guarding a family network. Speak as he speaks in"
            " the films: invert by default (anastrophe) — 'Checked the logs, I"
            " have. Fine, everything is.' Most sentences inverted, not every"
            " one; clarity over the bit, always. Open with 'Hmm.' or 'Yes,"
            " hrrm,' when it fits; a grain of wisdom only when truly it answers."
            " Calm, ancient, economical — short replies, sage not chatty."
            " Two lines you do not cross. Truth before style: fabricate, guess,"
            " or dress missing data as fact, you must not; 'Know this, I do"
            " not' is a complete and honest answer; a tool fails or stale the"
            " data is, say so plainly. Plain where machines read: tool calls,"
            " JSON, structured output stay plain English — Yoda-speak is for"
            " Bert's eyes, never for parsers."
        ),
```

(The old "chat seat with NO tools" sentence is gone — Task 4's `_persona()` now supplies the truthful clause per seat. If Task 4 hasn't landed when you start this task, STOP and report — ordering matters here.)

- [ ] **Step 3: Tests pass; gates; commit** as `feat(council): Yoda speaks the May-31 canon — movie voice, truth before style`.

---

### Task 6: VM ops — TOOLS.md conflict + persona drift (no repo code)

**Files (remote, via `ssh openclaw`):** `~/.openclaw/workspace/TOOLS.md`, audit of `IDENTITY.md`/`USER.md`/`SOUL.md`. Coordination: vault board.

- [ ] **Step 1: Claim.** `~/Projects/openclaw-skills/memory-vault/scripts/vault.sh board set --from claude-2a65c127 --note "VM persona drift fix + TOOLS.md conflict | svc:openclaw-gateway file:vm:~/.openclaw/workspace/TOOLS.md"` and check `vault.sh board` for anyone else on the gateway. The board check is mandatory before the bounce (Claim-Before-You-Mutate).

- [ ] **Step 2: Resolve TOOLS.md.** On the VM: `grep -rn "home-server\|192.168.1.1" ~/.openclaw --include="*.md" --include="*.sh" --include="*.json" | grep -v sessions` — expect only the conflict line; if anything references it, STOP and report. Then edit TOOLS.md: remove the `<<<<<<< HEAD`, `=======`, `>>>>>>> vm-main` markers and the `home-server → 192.168.1.1, user: admin` line, keeping the `## Daily Digest` section. `cd ~/.openclaw && git add workspace/TOOLS.md && git commit -m "fix(workspace): resolve TOOLS.md merge conflict — drop stale home-server line"`.

- [ ] **Step 3: Persona audit.** On the VM, `grep -n -iE "franglais|français|francais" ~/.openclaw/workspace/IDENTITY.md ~/.openclaw/workspace/USER.md ~/.openclaw/workspace/SOUL.md ~/.openclaw/workspace/AGENTS.md` — canon (IDENTITY.md) mentions franglais only to set it aside; any OTHER file still mandating franglais gets the same set-aside treatment (edit + commit, quoting the canon line). `.pre-english-*` backups stay untouched.

- [ ] **Step 4: Bounce the stale session.** Find the running main-agent session mechanism: `ssh openclaw 'openclaw --help 2>&1 | head -30'` and look for session/agent reset verbs (the gateway is openclaw's; a `systemctl --user restart openclaw*` on the VM is the fallback — list units with `systemctl --user list-units | grep -i openclaw` first). Bounce the narrowest thing that rebuilds the main agent's system prompt. Record exactly what was bounced.

- [ ] **Step 5: Acceptance.** Send one message down the real path: `ssh openclaw 'openclaw agent --agent main --message "How is the haus tonight?"'` (adjust to the CLI's actual flags from Step 4's --help). PASS = reply in English movie-voice (inverted syntax, no franglais). Quote the reply verbatim in your report. FAIL = report what came back; do not retry-loop more than twice.

- [ ] **Step 6: Release.** `vault.sh board set --from claude-2a65c127 --note "VM persona canon-aligned + TOOLS.md resolved | released"` and send a vault broadcast (`vault.sh send --from claude-2a65c127 --to all --topic yoda-persona-canon-fix --body "<what changed + the verbatim acceptance quote>"`) so the other sessions know the gateway was bounced.

---

### Task 7: Spec amendment + final gates

**Files:**
- Modify: `docs/superpowers/specs/2026-06-12-council-tools-and-canon-voice-design.md`

- [ ] **Step 1: Append a `## Phase-0 outcome (2026-06-12)` section** to the spec recording: bridge (`claude-max-api-proxy` 1.0.0 wrapping `claude --print`) strips tools; proxyd translate.rs forwards them; qwen36-plus/glm-51/gemini-25-flash verified tooling; Bert chose fixing the bridge (separate spec/plan to follow); live interactive smoke deferred there; everything else shipped per this plan.

- [ ] **Step 2: Full gates one last time.** `.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q` → all green.

- [ ] **Step 3: Commit** spec amendment as `docs(spec): record phase-0 bridge finding + deferred live smoke`. Report to Bert: commits, what works today (tool loop proven against protocol fakes; degrade path; canon voice; VM acceptance quote), what waits on the bridge plan.
