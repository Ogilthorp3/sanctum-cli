# Yoda Hybrid Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CLI Yoda fully tool-operational by running the tool loop on Gemini 3.1 Pro (which can tool) and voicing the answer on Opus via Max (which cannot tool but holds the canon voice).

**Architecture:** A new optional `Seat.tool_model` turns an armed seat into a two-stage turn: **gather** runs the existing hardened tool loop on `tool_model` (Gemini) and records the findings; **voice** flattens those findings to plain text and streams the canon answer on `seat.model` (Opus). The model override is achieved with `dataclasses.replace(seat, model=tool_model)` so no transport signature changes and the existing tool-loop tests stay green.

**Tech Stack:** Python 3.12, `rich`, `httpx`, `typer`, pytest. Transport is proxyd `:4040` (Anthropic dialect); Gemini tool-calling rides proxyd's `translate.rs`, Opus rides the `:3456` Max bridge (plain text only).

---

## Reference: current shapes (read before starting)

- `Seat` (frozen dataclass): `label, model, persona, style, verb, tools: tuple[str,...]=()`.
- `_tool_turn(seat, messages) -> str` — the hardened buffered loop; returns the final answer string. Calls `_post_with_tools(seat, convo, system=_persona(seat), tools=specs)` and `council_tools.run_tool(...)`.
- `_post_with_tools(seat, messages, *, system, tools) -> dict` — uses `seat.model`; raises `ToolsRejected` on 4xx.
- `_stream(seat, messages, *, system) -> Iterator[str]` — SSE deltas; uses `seat.model`.
- `_persona(seat, *, armed: bool | None = None) -> str` — armed=None derives from `seat.tools`; `armed=False` forces the no-tools clause.
- `_say_turn(seat, transcript, raw_arg) -> None` — adds the user msg, then: armed → `_tool_turn` (buffered, with ToolsRejected→stream fallback); else streaming via `_stream`.
- `Transcript.messages() -> list[dict[str,str]]`.
- `council_tools.run_tool(name, params, *, seat, session) -> ToolResult(content, is_error)` — already audits + redacts.
- Tests: `TestToolLoop` drives `_tool_turn` with `monkeypatch.setattr(cc, "_post_with_tools", fake)`; fakes have signature `def fake(seat, messages, *, system, tools)`. `TestSayTurn` drives `_say_turn` and monkeypatches `_tool_turn` / `_stream`.

Add `from dataclasses import replace` where needed (the module already imports `dataclass, field`).

---

### Task 1: `Seat.tool_model` field

**Files:**
- Modify: `sanctum_cli/commands/council.py` (the `Seat` dataclass)
- Test: `tests/test_council.py`

- [ ] **Step 1: Write the failing test**

Append to `class TestSeatTools` in `tests/test_council.py`:

```python
    def test_tool_model_defaults_none_and_is_optional(self) -> None:
        # Unarmed seats carry no tool_model.
        assert cc.SEATS["windu"].tool_model is None
        # The field exists and is constructible.
        s = cc.Seat(label="X", model="m", persona="p", style="white", verb="thinks")
        assert s.tool_model is None
        s2 = cc.Seat(
            label="X", model="m", persona="p", style="white", verb="thinks",
            tool_model="gemini-31-pro",
        )
        assert s2.tool_model == "gemini-31-pro"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k tool_model_defaults`
Expected: FAIL — `TypeError: Seat.__init__() got an unexpected keyword argument 'tool_model'` (or AttributeError).

- [ ] **Step 3: Add the field**

In `council.py`, add the field to `Seat` (after `tools`):

```python
@dataclass(frozen=True)
class Seat:
    """One council chair: a persona riding a proxyd model."""

    label: str
    model: str
    persona: str
    style: str  # rich color for the nameplate
    verb: str  # what the seat does while it thinks ("ponders", …)
    tools: tuple[str, ...] = ()
    # When set on an armed seat, tool turns run the tool loop on THIS model
    # (which can tool) and the final answer is voiced on `model` (which may
    # not). Unset → the armed loop runs entirely on `model`.
    tool_model: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k tool_model_defaults`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/council.py tests/test_council.py
git add sanctum_cli/commands/council.py tests/test_council.py
git commit -m "feat(council): add optional Seat.tool_model field"
```

---

### Task 2: `ToolExchange`/`ToolLoopResult` + extract `_run_tool_loop`, `_tool_turn` becomes a wrapper

**Files:**
- Modify: `sanctum_cli/commands/council.py` (`_tool_turn` → `_run_tool_loop` + wrapper; add dataclasses)
- Test: `tests/test_council.py`

The loop body moves into `_run_tool_loop(seat, messages) -> ToolLoopResult`, which returns the final answer string **and** the recorded tool exchanges. `_tool_turn` becomes `return _run_tool_loop(...).answer`, so every existing `TestToolLoop` test passes unchanged.

- [ ] **Step 1: Write the failing test**

Append to `class TestToolLoop` in `tests/test_council.py`:

```python
    def test_run_tool_loop_records_exchanges(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_run_tool_loop returns both the final answer and the executed
        tool exchanges (name, params, result) for the voice phase to use."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        monkeypatch.setattr(
            cc,
            "_post_with_tools",
            self._fake_responses(
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "logs_tail",
                            "input": {"service": "r2d2"},
                        }
                    ],
                },
                {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "Checked, I have."}],
                },
            ),
        )
        result = cc._run_tool_loop(cc.SEATS["yoda"], [{"role": "user", "content": "logs?"}])
        assert result.answer == "Checked, I have."
        assert len(result.exchanges) == 1
        ex = result.exchanges[0]
        assert ex.tool == "logs_tail"
        assert ex.params == {"service": "r2d2"}
        assert isinstance(ex.result, str) and ex.result  # the (redacted) tool output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k run_tool_loop_records`
Expected: FAIL — `AttributeError: module 'sanctum_cli.commands.council' has no attribute '_run_tool_loop'`.

- [ ] **Step 3: Add the dataclasses + rename the loop + add a wrapper**

In `council.py`, add near the other dataclasses (after `Seat`):

```python
@dataclass(frozen=True)
class ToolExchange:
    """One executed instrument call, kept so the voice phase can summarize it."""

    tool: str
    params: dict[str, object]
    result: str  # already redacted by run_tool
    is_error: bool


@dataclass(frozen=True)
class ToolLoopResult:
    """The gather phase's output: the model's final text plus what it gathered."""

    answer: str
    exchanges: tuple[ToolExchange, ...]
```

Rename `def _tool_turn(seat, messages) -> str:` to `def _run_tool_loop(seat, messages) -> ToolLoopResult:`. Inside it:
- Add `exchanges: list[ToolExchange] = []` next to `results`.
- After a well-formed tool actually runs (right after the `run_tool(...)` call and the `consecutive_errors` update), record the exchange:

```python
            consecutive_errors = consecutive_errors + 1 if result.is_error else 0
            exchanges.append(
                ToolExchange(
                    tool=str(block_name),
                    params=dict(block_input or {}),
                    result=result.content,
                    is_error=result.is_error,
                )
            )
```

- Change every `return <str>` in the loop to `return ToolLoopResult(answer=<str>, exchanges=tuple(exchanges))`. There are these return sites — convert each: the `MAX_TOOL_LOOP_ITERATIONS` backstop, the `stop_reason != tool_use` final-answer, the `budget_hit` breaker/cap pair, and the empty-results `"(no usable tool calls — answering without tools)"`.

Then add the back-compat wrapper immediately below:

```python
def _tool_turn(seat: Seat, messages: list[dict[str, object]]) -> str:
    """Back-compat: armed seats without a tool_model run the loop on
    seat.model and want just the final answer string."""
    return _run_tool_loop(seat, messages).answer
```

- [ ] **Step 4: Run tests to verify pass (new + all existing loop tests)**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k "TestToolLoop"`
Expected: PASS — the new exchanges test plus every pre-existing `_tool_turn` test (back-compat preserved).

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/council.py tests/test_council.py
.venv/bin/ruff check sanctum_cli/commands/council.py tests/test_council.py
.venv/bin/mypy sanctum_cli/commands/council.py
git add sanctum_cli/commands/council.py tests/test_council.py
git commit -m "refactor(council): extract _run_tool_loop returning findings; _tool_turn wraps it"
```

---

### Task 3: `_flatten_findings` — findings → plain text for the voice model

**Files:**
- Modify: `sanctum_cli/commands/council.py`
- Test: `tests/test_council.py`

The voice model (Opus via Max) can't take the tool protocol, so findings become a bounded plain-text block. Results are already redacted; this only formats and byte-caps them.

- [ ] **Step 1: Write the failing test**

Append a new test class to `tests/test_council.py`:

```python
class TestFlattenFindings:
    def test_flattens_to_labeled_block(self) -> None:
        exchanges = (
            cc.ToolExchange(tool="agent_list", params={}, result="3 agents OK", is_error=False),
            cc.ToolExchange(
                tool="logs_tail", params={"service": "r2d2"}, result="last line: ok", is_error=False
            ),
        )
        text = cc._flatten_findings(exchanges)
        assert "agent_list" in text and "3 agents OK" in text
        assert "logs_tail" in text and "r2d2" in text and "last line: ok" in text

    def test_byte_budget_truncates_long_results(self) -> None:
        huge = "x" * 50_000
        exchanges = (cc.ToolExchange(tool="logs_tail", params={}, result=huge, is_error=False),)
        text = cc._flatten_findings(exchanges)
        assert len(text.encode("utf-8")) <= cc.FINDINGS_MAX_BYTES + 200  # block + marker headroom
        assert "truncated" in text.lower()

    def test_empty_exchanges_is_empty_string(self) -> None:
        assert cc._flatten_findings(()) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k TestFlattenFindings`
Expected: FAIL — `AttributeError: ... has no attribute '_flatten_findings'` / `FINDINGS_MAX_BYTES`.

- [ ] **Step 3: Implement**

In `council.py`, add a constant near `TOOL_CALL_CAP`:

```python
# Byte budget for the flattened findings block handed to the voice model.
# Bounds the voice prompt so a chatty tool result can't blow it up.
FINDINGS_MAX_BYTES = 8_000
```

And the function (place it just below `ToolLoopResult`):

```python
def _flatten_findings(exchanges: tuple[ToolExchange, ...]) -> str:
    """Render gathered tool results as a plain-text block for the voice model.

    Results are already redacted by run_tool; this formats and byte-caps them
    so the voice prompt stays bounded. Returns '' when nothing was gathered.
    """
    if not exchanges:
        return ""
    lines = ["The instruments returned (use these facts; do not invent others):"]
    for ex in exchanges:
        params = f"({ex.params})" if ex.params else ""
        tag = " [error]" if ex.is_error else ""
        lines.append(f"- {ex.tool}{params}{tag} → {ex.result}")
    block = "\n".join(lines)
    data = block.encode("utf-8", errors="replace")
    if len(data) > FINDINGS_MAX_BYTES:
        block = data[:FINDINGS_MAX_BYTES].decode("utf-8", errors="replace")
        block += "\n[findings truncated to fit the voice budget]"
    return block
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k TestFlattenFindings`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/council.py tests/test_council.py
git add sanctum_cli/commands/council.py tests/test_council.py
git commit -m "feat(council): _flatten_findings — bounded plain-text findings for the voice model"
```

---

### Task 4: `_stream_and_print` extraction + `_gather_then_voice` + `_say_turn` dispatch

**Files:**
- Modify: `sanctum_cli/commands/council.py` (`_say_turn`; add `_stream_and_print`, `_gather_then_voice`)
- Test: `tests/test_council.py`

This is the wiring. Extract the streaming-and-print logic so both the unarmed path and the voice phase reuse it, then add the two-stage helper and dispatch on `tool_model`.

- [ ] **Step 1: Write the failing tests**

Append to `class TestSayTurn` in `tests/test_council.py`:

```python
    def test_gather_then_voice_opus_voices_gemini_findings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An armed seat with a tool_model gathers on the tool_model then
        voices on seat.model. The voice model must receive the flattened
        findings as TEXT (never tool_use/tool_result blocks) and its streamed
        text is the answer."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        seat = cc.Seat(
            label="Yoda", model="opus-voice", persona="P", style="green",
            verb="ponders", tools=("agent_list",), tool_model="gemini-hands",
        )

        # gather (gemini): one tool call, then end_turn
        monkeypatch.setattr(
            cc,
            "_post_with_tools",
            self._fake_responses(
                {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "t1", "name": "agent_list", "input": {}}],
                },
                {"stop_reason": "end_turn", "content": [{"type": "text", "text": "gemini text"}]},
            ),
        )

        seen: dict = {}

        def fake_stream(s, messages, *, system):  # type: ignore[misc]
            seen["model"] = s.model
            seen["system"] = system
            seen["messages"] = messages
            yield "Healthy, the agents are."

        monkeypatch.setattr(cc, "_stream", fake_stream)
        transcript = cc.Transcript()
        cc._say_turn(seat, transcript, "agents ok?")

        # Voiced on seat.model, not the tool_model
        assert seen["model"] == "opus-voice"
        # Voice model got TEXT only — no tool protocol leaked into the messages
        blob = repr(seen["messages"])
        assert "tool_use" not in blob and "tool_result" not in blob
        # The findings text reached the voice prompt
        assert "agent_list" in blob
        # Unarmed persona for the voice phase (no instruments clause)
        assert "Instruments you have" not in seen["system"]
        # The streamed text is the recorded answer
        assert transcript.messages()[-1]["content"] == "Healthy, the agents are."

    def test_no_tool_turn_is_voice_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When the tool_model calls no tools, it's effectively voice-only:
        the voice model answers the bare question (no findings block)."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        seat = cc.Seat(
            label="Yoda", model="opus-voice", persona="P", style="green",
            verb="ponders", tools=("agent_list",), tool_model="gemini-hands",
        )
        monkeypatch.setattr(
            cc, "_post_with_tools",
            self._fake_responses(
                {"stop_reason": "end_turn", "content": [{"type": "text", "text": "no tools used"}]},
            ),
        )
        captured: dict = {}

        def fake_stream(s, messages, *, system):  # type: ignore[misc]
            captured["messages"] = messages
            yield "Quiet, the haus is."

        monkeypatch.setattr(cc, "_stream", fake_stream)
        transcript = cc.Transcript()
        cc._say_turn(seat, transcript, "how is it?")
        # No findings block injected — the last user msg is the bare question
        assert captured["messages"][-1]["content"] == "how is it?"
        assert transcript.messages()[-1]["content"] == "Quiet, the haus is."

    def test_voice_failure_falls_back_to_gathered_answer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If the voice model errors, return the tool_model's own final text
        rather than nothing."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        seat = cc.Seat(
            label="Yoda", model="opus-voice", persona="P", style="green",
            verb="ponders", tools=("agent_list",), tool_model="gemini-hands",
        )
        monkeypatch.setattr(
            cc, "_post_with_tools",
            self._fake_responses(
                {"stop_reason": "end_turn", "content": [{"type": "text", "text": "gathered fallback"}]},
            ),
        )

        def boom_stream(s, messages, *, system):  # type: ignore[misc]
            raise RuntimeError("voice bridge down [/bad]")
            yield  # generator

        monkeypatch.setattr(cc, "_stream", boom_stream)
        transcript = cc.Transcript()
        cc._say_turn(seat, transcript, "q")  # must not raise (markup-safe)
        assert transcript.messages()[-1]["content"] == "gathered fallback"

    def test_gather_tools_rejected_falls_back_to_chat(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If the tool_model rejects tools (4xx), degrade to a plain chat
        answer on the voice model with the unarmed persona."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        seat = cc.Seat(
            label="Yoda", model="opus-voice", persona="P", style="green",
            verb="ponders", tools=("agent_list",), tool_model="gemini-hands",
        )

        def reject(seat_, messages, *, system, tools):  # type: ignore[misc]
            raise cc.ToolsRejected("HTTP 400")

        monkeypatch.setattr(cc, "_post_with_tools", reject)

        def fake_stream(s, messages, *, system):  # type: ignore[misc]
            assert "Instruments you have" not in system  # unarmed clause
            yield "Chat only, this turn."

        monkeypatch.setattr(cc, "_stream", fake_stream)
        transcript = cc.Transcript()
        cc._say_turn(seat, transcript, "q")
        assert transcript.messages()[-1]["content"] == "Chat only, this turn."
```

Also UPDATE the existing `test_ctrl_c_aborts_tool_turn_not_session` so it targets the single-model path explicitly (a seat with tools but no tool_model), since real Yoda will gain a tool_model in Task 5:

```python
    def test_ctrl_c_aborts_tool_turn_not_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KeyboardInterrupt from the armed (no-tool_model) loop must yield
        '(turn aborted)' in the transcript, not a propagated exception."""
        def raising(seat, messages):  # type: ignore[misc]
            raise KeyboardInterrupt

        monkeypatch.setattr(cc, "_tool_turn", raising)
        transcript = cc.Transcript()
        seat = cc.replace(cc.SEATS["yoda"], tool_model=None)  # force the single-model path
        cc._say_turn(seat, transcript, "what is status?")
        assert transcript.messages()[-1]["content"] == "(turn aborted)"
```

(If `cc.replace` isn't exported, use `dataclasses.replace`: add `from dataclasses import replace` to the test imports and call `replace(cc.SEATS["yoda"], tool_model=None)`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k "TestSayTurn"`
Expected: FAIL — the new gather/voice behaviors aren't implemented; the ctrl-c test fails on `cc.replace`/routing.

- [ ] **Step 3: Implement — extract `_stream_and_print`, add `_gather_then_voice`, rewrite `_say_turn` dispatch**

Add `from dataclasses import replace` to the council.py imports (it currently imports `dataclass, field`).

Add `_stream_and_print` (place above `_say_turn`):

```python
def _stream_and_print(seat: Seat, messages: list[dict[str, str]], *, system: str) -> str:
    """Stream a seat's reply on seat.model, printing deltas markup-safe, and
    return the joined text. KeyboardInterrupt prints '(turn aborted)' and
    returns it. Transport errors propagate — the caller owns the fallback.
    """
    chunks: list[str] = []
    stream = _stream(seat, messages, system=system)
    try:
        with console.status(
            thinking_markup(seat), spinner="simpleDotsScrolling", spinner_style=seat.style
        ):
            first = next(stream, None)
    except KeyboardInterrupt:
        console.print("\n[dim](turn aborted)[/]")
        return "(turn aborted)"
    console.print(f"[{seat.style}]{seat.label}:[/] ", end="")
    if first is not None:
        chunks.append(first)
        console.print(Text(first), end="", soft_wrap=True)
        try:
            for delta in stream:
                chunks.append(delta)
                console.print(Text(delta), end="", soft_wrap=True)
        except KeyboardInterrupt:
            console.print("\n[dim](turn aborted)[/]")
            return "".join(chunks) if chunks else "(turn aborted)"
    console.print()
    answer = "".join(chunks)
    return answer if answer else "(no answer)"
```

Add `_gather_then_voice` (place above `_say_turn`):

```python
def _gather_then_voice(seat: Seat, transcript: Transcript, raw_arg: str) -> None:
    """Two-stage armed turn: gather facts on seat.tool_model (which can tool),
    then voice the answer on seat.model (the canon voice). The voice model
    never sees the tool protocol — findings are flattened to plain text.
    """
    gather_seat = replace(seat, model=seat.tool_model or seat.model)
    try:
        findings = _run_tool_loop(
            gather_seat, cast("list[dict[str, object]]", transcript.messages())
        )
    except KeyboardInterrupt:
        console.print("\n[dim](turn aborted)[/]")
        transcript.add("assistant", "(turn aborted)")
        return
    except ToolsRejected:
        # tool model won't tool — degrade to a plain chat answer on the voice model
        try:
            answer = _stream_and_print(
                seat, transcript.messages(), system=_persona(seat, armed=False)
            )
        except Exception as e:
            console.print(f"\n[red]⚠ {escape(str(e))}[/]")
            transcript.add("assistant", "(seat unavailable)")
            return
        transcript.add("assistant", answer)
        return
    except Exception as e:
        console.print(f"[red]⚠ {escape(str(e))}[/]")
        transcript.add("assistant", "(seat unavailable)")
        return

    # Build the voice turn: question + flattened findings in the last user msg.
    voice_msgs = transcript.messages()
    block = _flatten_findings(findings.exchanges)
    if block:
        voice_msgs = voice_msgs[:-1] + [{"role": "user", "content": f"{raw_arg}\n\n{block}"}]
    try:
        answer = _stream_and_print(seat, voice_msgs, system=_persona(seat, armed=False))
    except Exception:
        # Voice model down — fall back to what the tool model gathered/said.
        answer = findings.answer or "(no answer)"
        console.print(Text(f"{seat.label}: {answer}"), soft_wrap=True)
    transcript.add("assistant", answer)
```

Rewrite `_say_turn` to dispatch and reuse `_stream_and_print` for the unarmed path. Replace the entire body of `_say_turn` with:

```python
def _say_turn(
    seat: Seat,
    transcript: Transcript,
    raw_arg: str,
) -> None:
    """Execute one say/switch_say turn. Three paths: two-stage gather+voice
    (armed seat with a tool_model), single-model buffered tool loop (armed,
    no tool_model), or plain streaming chat (unarmed)."""
    transcript.add("user", raw_arg)

    if seat.tools and seat.tool_model:
        _gather_then_voice(seat, transcript, raw_arg)
        return

    if seat.tools:
        try:
            try:
                answer = _tool_turn(seat, cast("list[dict[str, object]]", transcript.messages()))
            except KeyboardInterrupt:
                console.print("\n[dim](turn aborted)[/]")
                transcript.add("assistant", "(turn aborted)")
                return
            answer = answer if answer else "(no answer)"
            nameplate = Text.from_markup(f"[{seat.style}]{seat.label}:[/] ")
            console.print(nameplate.append_text(Text(answer)), soft_wrap=True)
            transcript.add("assistant", answer)
            return
        except ToolsRejected:
            console.print("[dim](seat's model declines tools — chat only this turn)[/]")
            # fall through to streaming with the unarmed persona
        except Exception as e:
            console.print(f"[red]⚠ {escape(str(e))}[/]")
            transcript.add("assistant", "(seat unavailable)")
            return

    persona_system = _persona(seat, armed=False) if seat.tools else _persona(seat)
    try:
        answer = _stream_and_print(seat, transcript.messages(), system=persona_system)
    except Exception as e:
        console.print(f"\n[red]⚠ {escape(str(e))}[/]")
        transcript.add("assistant", "(seat unavailable)")
        return
    transcript.add("assistant", answer)
```

- [ ] **Step 4: Run tests to verify pass (new + all of TestSayTurn + streaming tests)**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k "TestSayTurn or streaming"`
Expected: PASS — gather/voice, no-tool-only, voice-failure fallback, tools-rejected fallback, the updated ctrl-c test, and the pre-existing markup/empty-answer/streaming tests.

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/council.py tests/test_council.py
.venv/bin/ruff check sanctum_cli/commands/council.py tests/test_council.py
.venv/bin/mypy sanctum_cli/commands/council.py
git add sanctum_cli/commands/council.py tests/test_council.py
git commit -m "feat(council): two-stage gather-then-voice for tool_model seats"
```

---

### Task 5: Arm Yoda + Mon Mothma with `tool_model="gemini-31-pro"`

**Files:**
- Modify: `sanctum_cli/commands/council.py` (the `yoda` and `mothma` entries in `SEATS`)
- Test: `tests/test_council.py`

- [ ] **Step 1: Write the failing test**

Append to `class TestSeatTools`:

```python
    def test_armed_seats_use_gemini_hands(self) -> None:
        for k in ("yoda", "mothma"):
            assert cc.SEATS[k].tools, f"{k} should be armed"
            assert cc.SEATS[k].tool_model == "gemini-31-pro", f"{k} gathers on Gemini"
        # Unarmed seats never get a tool_model.
        for k, s in cc.SEATS.items():
            if not s.tools:
                assert s.tool_model is None, f"{k} is unarmed; no tool_model"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k armed_seats_use_gemini`
Expected: FAIL — `tool_model` is `None` for yoda/mothma.

- [ ] **Step 3: Set the field on both armed seats**

In `SEATS`, add `tool_model="gemini-31-pro",` to the `yoda` seat (next to its `tools=(...)`) and to the `mothma` seat (next to its `tools=(...)`). Add a comment above the yoda one:

```python
        # Gather on Gemini 3.1 Pro (tools work via proxyd translation), voice on
        # council-max-thinking (Opus/Max). See the 2026-06-13 hybrid-transport spec.
        tool_model="gemini-31-pro",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_council.py -q -k "armed_seats_use_gemini or TestSeatTools"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/council.py tests/test_council.py
git add sanctum_cli/commands/council.py tests/test_council.py
git commit -m "feat(council): arm Yoda + Mon Mothma with gemini-31-pro tool_model"
```

---

### Task 6: Final gates + live smoke (gated on billing) + report

**Files:** none (verification only)

- [ ] **Step 1: Full gates under the memory watchdog**

Run:
```bash
cd /Users/bert/Projects/sanctum-cli
.venv/bin/ruff check sanctum_cli tests
.venv/bin/mypy sanctum_cli
/tmp/guarded_pytest.sh   # the 3GB-RSS-watchdog runner from the 2026-06-12 session
```
Expected: ruff clean; mypy clean; full suite green; watchdog never trips. (If `/tmp/guarded_pytest.sh` is gone, recreate it: run `.venv/bin/python -m pytest -q` with a background process that kills the pytest tree if its RSS exceeds 3 GB.)

- [ ] **Step 2: Confirm the billing assumption with Bert (BLOCKING for the live smoke only)**

Ask Bert to confirm the Google AI Studio Ultra sub covers `gemini-3.1-pro` API calls flat-rate (vs. metered billing on the `gemini-api-key`). Do NOT run the live smoke until confirmed — a metered key turns every tool turn into per-token spend.

- [ ] **Step 3: Live smoke (only after Step 2 confirms)**

Run one real armed turn and capture Yoda's reply:
```bash
cd /Users/bert/Projects/sanctum-cli
printf '/yoda check the agents and tell me how the haus is\n/quit\n' | .venv/bin/sanctum council
```
PASS = Gemini calls `agent_list` (an audit line appears in `~/.sanctum/logs/council-tools-audit.jsonl` with `seat=Yoda`), and Opus voices the result in canon English (inverted syntax). Quote the reply verbatim in the report. FAIL = report what came back; do not retry-loop more than twice.

- [ ] **Step 4: Report to Bert**

Summarize: commits, what works (two-stage gather/voice proven against fakes; degrade paths; back-compat), the live-smoke quote (or that it's pending billing confirm), and the documented follow-up (the optional tool-need gate to skip Gemini on obvious chat turns).

---

## Self-Review

**Spec coverage:**
- Seat.tool_model (spec A) → Task 1. ✓
- Gather phase reusing the hardened loop + recording findings (spec B) → Task 2. ✓
- `_voice_answer`/voice phase with unarmed persona + flattened findings (spec C) → Tasks 3 (flatten) + 4 (voice via `_stream_and_print`). ✓
- `_say_turn` dispatch (spec D) → Task 4. ✓
- proxyd config-only/no Rust (spec E) → no proxyd task needed; `gemini-31-pro` referenced directly (Task 5). ✓
- Data flow (gemini gather / opus voice) → Task 4 wiring. ✓
- Error handling: gemini ToolsRejected → Opus chat; voice fail → gathered answer; Ctrl-C → aborted; markup-safe → all covered in Task 4 tests + code. ✓
- Non-goals: no gate (documented Task 6 Step 4), no fan-out tooling (untouched), no mutate tools (untouched), no proxyd Rust (none). ✓
- Testing: gather records findings (T2), voice gets text not protocol (T4), no-tool=opus-only (T4), flattening faithful+bounded (T3), degrade paths (T4), back-compat (T2/T4), live smoke (T6). ✓
- Risks: 2-round-trips (accepted), gemini preview (degrades), billing (Task 6 Step 2 gate), flattening loses structure (acceptable). ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; commands have expected output. ✓

**Type consistency:** `ToolExchange(tool, params, result, is_error)` and `ToolLoopResult(answer, exchanges)` used identically in Tasks 2–4. `_run_tool_loop -> ToolLoopResult`, `_tool_turn -> str`, `_flatten_findings(tuple[ToolExchange,...]) -> str`, `_stream_and_print(seat, messages, *, system) -> str`, `_gather_then_voice(seat, transcript, raw_arg) -> None` — consistent across tasks. `tool_model` field name consistent. ✓
