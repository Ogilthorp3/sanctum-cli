"""``sanctum council`` — the Jedi Council chamber in a terminal.

Interactive REPL (seat-switching, shared transcript) + one-shot fan-out mode
(``sanctum council "question"`` asks every seat in parallel and ends with a
Yoda synthesis). Seats are proxyd :4040 council models (Anthropic dialect);
each Jedi is a persona system-prompt on top of a seat — Yoda and Mundi share
a brain but not a voice, which is the neurodiversity doctrine in one line.

Tests cover the pure parts (registry, REPL parsing, transcript, SSE delta
parsing, fan-out aggregation with a stubbed transport). The network client
is exercised against a stub — never a live proxyd.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sanctum_cli.commands import council as cc

if TYPE_CHECKING:
    from pathlib import Path


class TestSeats:
    def test_roster_shape_and_doctrine(self) -> None:
        assert set(cc.SEATS) == {"yoda", "windu", "quigon", "mundi", "cilghal", "jocasta", "mothma"}
        for jedi, seat in cc.SEATS.items():
            assert seat.label and seat.model and seat.persona, jedi
        # Yoda + Mundi share a model but never a persona (neurodiversity).
        assert cc.SEATS["yoda"].model == cc.SEATS["mundi"].model
        assert cc.SEATS["yoda"].persona != cc.SEATS["mundi"].persona
        assert cc.DEFAULT_SEAT == "yoda"


class TestReplParsing:
    @pytest.mark.parametrize(
        ("line", "kind", "arg"),
        [
            ("hello there", "say", "hello there"),
            ("/windu", "switch", "windu"),
            ("/windu  what about DoH?", "switch_say", "what about DoH?"),
            ("/council should we buy an AP7?", "council", "should we buy an AP7?"),
            ("@council should we?", "council", "should we?"),
            ("/seats", "seats", ""),
            ("/new", "new", ""),
            ("/quit", "quit", ""),
            ("/exit", "quit", ""),
            ("", "noop", ""),
            ("   ", "noop", ""),
        ],
    )
    def test_parse(self, line: str, kind: str, arg: str) -> None:
        action = cc.parse_repl_input(line)
        assert action.kind == kind
        assert action.arg == arg

    def test_unknown_slash_is_error_not_chat(self) -> None:
        action = cc.parse_repl_input("/anakin hello")
        assert action.kind == "error"
        assert "anakin" in action.arg


class TestTranscript:
    def test_append_and_cap(self) -> None:
        t = cc.Transcript(max_turns=3)
        for i in range(10):
            t.add("user", f"q{i}")
            t.add("assistant", f"a{i}")
        msgs = t.messages()
        assert len(msgs) == 6  # 3 turns = 3 user + 3 assistant
        assert msgs[0]["content"] == "q7"
        assert msgs[-1]["content"] == "a9"

    def test_clear(self) -> None:
        t = cc.Transcript()
        t.add("user", "q")
        t.clear()
        assert t.messages() == []


class TestSseParsing:
    def test_extracts_text_deltas(self) -> None:
        events = [
            'data: {"type":"message_start","message":{}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}',
            "",
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}',
            'data: {"type":"message_stop"}',
        ]
        out = [d for d in (cc.sse_text_delta(e) for e in events) if d]
        assert out == ["Hel", "lo"]

    def test_garbage_lines_yield_nothing(self) -> None:
        assert cc.sse_text_delta("event: ping") is None
        assert cc.sse_text_delta("data: not-json{") is None
        assert cc.sse_text_delta('data: {"type":"message_stop"}') is None


class TestFanOut:
    def test_council_ask_collects_every_seat_and_synthesizes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str]] = []

        def fake_complete(seat: cc.Seat, messages: list[dict[str, str]], *, system: str) -> str:
            calls.append((seat.label, messages[-1]["content"]))
            if "synthesize" in messages[-1]["content"].lower():
                return "Synthesis: do it, we should."
            return f"{seat.label} says aye"

        monkeypatch.setattr(cc, "_complete", fake_complete)
        result = cc.council_ask("Should we build the portcullis?")
        # Every seat answered…
        assert set(result.answers) == {s.label for s in cc.SEATS.values()}
        assert result.answers["Windu"] == "Windu says aye"
        # …and Yoda synthesized from the collected answers.
        assert result.synthesis and "Synthesis" in result.synthesis
        synth_call = calls[-1]
        assert synth_call[0] == "Yoda"
        assert "Windu says aye" in synth_call[1]

    def test_one_dead_seat_never_sinks_the_council(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def flaky_complete(seat: cc.Seat, messages: list[dict[str, str]], *, system: str) -> str:
            if seat.label == "Cilghal":
                raise RuntimeError("seat offline")
            return "aye"

        monkeypatch.setattr(cc, "_complete", flaky_complete)
        result = cc.council_ask("quorum check")
        assert result.answers["Cilghal"].startswith("⚠")
        assert result.answers["Windu"] == "aye"
        assert result.synthesis  # synthesis still runs on the survivors


class TestThinkingIndicator:
    def test_every_seat_waits_in_character(self) -> None:
        # every seat has a verb, and the verb composes cleanly with the
        # ellipsis the markup helper appends
        for seat in cc.SEATS.values():
            assert seat.verb, f"{seat.label} has no thinking verb"
            assert seat.verb[-1] not in ".…!?", f"{seat.label} verb carries punctuation"

    def test_thinking_markup_carries_label_verb_and_colour(self) -> None:
        seat = cc.SEATS["yoda"]
        line = cc.thinking_markup(seat)
        assert "Yoda ponders…" in line
        assert f"[{seat.style}]" in line


class TestToolLoop:
    """The loop is tested against a faked transport (the bridge can't
    tool yet — phase-0 finding); the live smoke belongs to the bridge
    plan. The fake speaks the Anthropic protocol: content blocks +
    stop_reason."""

    @staticmethod
    def _fake_responses(*responses: dict) -> object:
        seq = iter(responses)

        def fake_post(seat, messages, *, system, tools):  # type: ignore[misc]
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

    def test_cap_stops_runaway_turns(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        endless = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "tu_n", "name": "agent_list", "input": {}}],
        }
        monkeypatch.setattr(cc, "_post_with_tools", self._fake_responses(*([endless] * 20)))
        answer = cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "loop!"}])
        audit_text = (tmp_path / "a.jsonl").read_text()
        audit_count = len(audit_text.splitlines())
        # Exactly TOOL_CALL_CAP audit lines — no more, no fewer.
        assert audit_count == cc.TOOL_CALL_CAP, (
            f"expected exactly {cc.TOOL_CALL_CAP} audit lines, got {audit_count}"
        )
        assert isinstance(answer, str), "a capped turn still returns an answer"
        assert answer == "(tool call cap reached — partial answer only)"

    def test_breaker_after_two_consecutive_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        bad_call = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "tu_e", "name": "nonexistent", "input": {}}],
        }
        final = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Hmm."}]}
        monkeypatch.setattr(cc, "_post_with_tools", self._fake_responses(bad_call, bad_call, final))
        answer = cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "q"}])
        assert answer == "Hmm."

    def test_breaker_third_call_never_executes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """After the breaker opens (2 consecutive errors), a third tool_use
        response must return the breaker string — the third call must NOT
        execute. Exactly 2 audit lines must exist (third never fires).

        This test kills the deletion-mutant: removing ``consecutive_errors >= 2``
        from the cap/breaker condition causes the turn to loop past 2 errors
        and execute a third call (producing 3 audit lines), which fails the
        assertion below.
        """
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        bad_call = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "tu_e", "name": "nonexistent", "input": {}}],
        }
        # Three bad_call responses — after the 2nd error the breaker should fire
        # and return the breaker string without making a 3rd POST at all.
        monkeypatch.setattr(
            cc, "_post_with_tools", self._fake_responses(bad_call, bad_call, bad_call)
        )
        answer = cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "q"}])
        assert answer == "(instrument errors — answering without further tools)", (
            f"expected breaker string, got: {answer!r}"
        )
        audit_lines = (tmp_path / "a.jsonl").read_text().splitlines()
        assert len(audit_lines) == 2, (
            f"expected exactly 2 audit lines (third call never executed), got {len(audit_lines)}"
        )

    def test_4xx_with_tools_degrades_to_chat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def reject(seat, messages, *, system, tools):  # type: ignore[misc]
            raise cc.ToolsRejected("HTTP 400: unknown field tools")

        monkeypatch.setattr(cc, "_post_with_tools", reject)
        with pytest.raises(cc.ToolsRejected):
            cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "q"}])
        # the REPL catches ToolsRejected and falls back to the streaming
        # path — asserted structurally: the except clause exists in _say_turn
        import inspect

        src = inspect.getsource(cc._say_turn)
        assert "ToolsRejected" in src

    def test_mid_loop_4xx_raises_runtime_not_tools_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 4xx that arrives AFTER the first successful POST must raise
        RuntimeError — not ToolsRejected — so it's not mislabelled as
        'seat declines tools' when context has already been gathered."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        first_ok = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "agent_list", "input": {}}],
        }

        call_count = 0

        def fake_post(seat, messages, *, system, tools):  # type: ignore[misc]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_ok
            raise cc.ToolsRejected("HTTP 400: mid-loop rejection")

        monkeypatch.setattr(cc, "_post_with_tools", fake_post)
        with pytest.raises(RuntimeError, match="mid-turn tools failure"):
            cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "q"}])
        # Must NOT be ToolsRejected — verified above by the pytest.raises(RuntimeError)
        assert call_count == 2

    def test_stringified_json_input_rescues_and_tool_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A tool_use block with ``"input": '{"service": "r2d2"}'`` (stringified JSON)
        must be json.loads-rescued so the tool actually runs.
        The final answer must come from the second model response."""
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
                            "id": "tu_str",
                            "name": "logs_tail",
                            # Stringified JSON input — the classic local-model malformation
                            "input": '{"service": "r2d2"}',
                        }
                    ],
                },
                {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "The logs look fine."}],
                },
            ),
        )
        seat = cc.SEATS["yoda"]
        answer = cc._tool_turn(seat, [{"role": "user", "content": "check r2d2 logs"}])
        # The tool ran (audit line exists) and the answer came from the model
        assert answer == "The logs look fine."
        audit_lines = (tmp_path / "a.jsonl").read_text().splitlines()
        assert len(audit_lines) == 1, "the rescued tool call was audited exactly once"

    def test_unparseable_stringified_input_feeds_error_result_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A tool_use block with ``"input": 'not valid json{'`` must NOT abort
        the turn. An audited error result must be fed back; the model's
        next response (answering from the error) must become the answer."""
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
                            "id": "tu_bad",
                            "name": "logs_tail",
                            "input": "not valid json{",
                        }
                    ],
                },
                {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "Cannot read the logs, I can."}],
                },
            ),
        )
        seat = cc.SEATS["yoda"]
        answer = cc._tool_turn(seat, [{"role": "user", "content": "check r2d2"}])
        assert answer == "Cannot read the logs, I can."
        # Audit line must exist for the malformed attempt
        audit_lines = (tmp_path / "a.jsonl").read_text().splitlines()
        assert len(audit_lines) == 1, "malformed block was audited"

    def test_missing_id_turn_survives_with_audit_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A tool_use block with no ``id`` field must not abort the turn.
        The block is audited (directly via council_tools.audit) but no
        tool_result is appended (no id to reference the result back to).
        The turn continues and the model's final answer is returned."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")

        # Block with no 'id' — has a name but missing the required id field
        no_id_block = {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    # no "id" key
                    "name": "agent_list",
                    "input": {},
                }
            ],
        }
        final = {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "No id, but I survived."}],
        }
        monkeypatch.setattr(cc, "_post_with_tools", self._fake_responses(no_id_block, final))
        seat = cc.SEATS["yoda"]
        answer = cc._tool_turn(seat, [{"role": "user", "content": "q"}])
        assert answer == "No id, but I survived."
        audit_lines = (tmp_path / "a.jsonl").read_text().splitlines()
        assert len(audit_lines) >= 1, "malformed block (missing id) was audited"
        import json as _json

        first_entry = _json.loads(audit_lines[0])
        # The name is known (agent_list) so it should be recorded; outcome is error
        # because the block is malformed (no id means we can't loop correctly)
        assert first_entry["tool"] == "agent_list"
        assert first_entry["outcome"] == "error"


class TestSeatTools:
    def test_yoda_and_mothma_are_armed_others_are_not(self) -> None:
        armed = {k for k, s in cc.SEATS.items() if s.tools}
        assert armed == {"yoda", "mothma"}
        for k in armed:
            assert set(cc.SEATS[k].tools) == {
                "sanctum_status",
                "sanctum_doctor",
                "agent_list",
                "logs_tail",
            }


class TestPersonaComposition:
    def test_armed_seats_get_instruments_clause(self) -> None:
        text = cc._persona(cc.SEATS["yoda"])
        assert "Instruments you have" in text
        assert "sanctum_status" in text

    def test_toolless_seats_keep_the_no_tools_truth(self) -> None:
        text = cc._persona(cc.SEATS["windu"])
        assert "NO tools" in text

    def test_armed_false_forces_unarmed_clause(self) -> None:
        """_persona(seat, armed=False) must produce the unarmed clause even
        for an armed seat like Yoda."""
        yoda = cc.SEATS["yoda"]
        text = cc._persona(yoda, armed=False)
        assert "NO tools" in text
        assert "Instruments" not in text

    def test_armed_false_for_yoda_contains_no_tools_not_instruments(self) -> None:
        """Specific breaker: the REPL ToolsRejected fallback must send the
        unarmed persona — verify the exact strings the spec names."""
        text = cc._persona(cc.SEATS["yoda"], armed=False)
        assert "NO tools" in text
        assert "Instruments you have" not in text


class TestSayTurn:
    """Unit tests for _say_turn — the extracted say-branch logic.

    These cover Ctrl-C abort, markup-safe printing, and empty-answer
    placeholder without wiring up an interactive REPL loop.
    """

    def test_ctrl_c_aborts_tool_turn_not_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KeyboardInterrupt from _tool_turn must result in '(turn aborted)'
        in the transcript, not a propagated exception."""

        def raising_tool_turn(seat, messages):  # type: ignore[misc]
            raise KeyboardInterrupt

        monkeypatch.setattr(cc, "_tool_turn", raising_tool_turn)
        transcript = cc.Transcript()
        seat = cc.SEATS["yoda"]
        # Must not raise — the abort is swallowed and logged
        cc._say_turn(seat, transcript, "what is status?")
        msgs = transcript.messages()
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == "(turn aborted)"

    def test_markup_in_answer_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An answer containing Rich markup like ``[/bad]`` must not raise
        MarkupError and must land verbatim in the transcript."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        nasty_answer = "The answer is [/bad] markup here."

        def fake_tool_turn(seat, messages):  # type: ignore[misc]
            return nasty_answer

        monkeypatch.setattr(cc, "_tool_turn", fake_tool_turn)
        transcript = cc.Transcript()
        seat = cc.SEATS["yoda"]
        # Must not raise despite malformed markup in the answer
        cc._say_turn(seat, transcript, "q")
        msgs = transcript.messages()
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == nasty_answer

    def test_empty_answer_becomes_placeholder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An empty answer from _tool_turn must produce '(no answer)' in
        the transcript, never an empty string."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")

        def fake_tool_turn(seat, messages):  # type: ignore[misc]
            return ""

        monkeypatch.setattr(cc, "_tool_turn", fake_tool_turn)
        transcript = cc.Transcript()
        seat = cc.SEATS["yoda"]
        cc._say_turn(seat, transcript, "q")
        msgs = transcript.messages()
        assert msgs[-1]["content"] == "(no answer)"
