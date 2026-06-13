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

from dataclasses import replace
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

    def test_rescued_input_echoed_as_dict_not_string(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """After a rescued-input turn, the SECOND POST's convo must carry the
        parsed dict as ``input`` (not the original string).  The echo-back of
        the assistant block is what feeds the next request — if it still holds
        a string the Anthropic protocol rejects it with a 400."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")

        captured_convos: list[list] = []

        def capturing_post(seat, messages, *, system, tools):  # type: ignore[misc]
            captured_convos.append([m for m in messages])
            call_n = len(captured_convos)
            if call_n == 1:
                return {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_rescue",
                            "name": "logs_tail",
                            "input": '{"service": "r2d2"}',  # stringified
                        }
                    ],
                }
            return {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done."}],
            }

        monkeypatch.setattr(cc, "_post_with_tools", capturing_post)
        seat = cc.SEATS["yoda"]
        answer = cc._tool_turn(seat, [{"role": "user", "content": "check r2d2"}])
        assert answer == "Done."
        # The second POST must have been made
        assert len(captured_convos) == 2, "expected exactly 2 POSTs"
        # Find the echoed assistant block in the second POST's messages
        second_convo = captured_convos[1]
        assistant_msgs = [m for m in second_convo if m.get("role") == "assistant"]
        assert assistant_msgs, "no assistant message in second POST"
        asst_content = assistant_msgs[-1]["content"]
        assert isinstance(asst_content, list), "assistant content must be a list"
        tool_use_blocks = [b for b in asst_content if b.get("type") == "tool_use"]
        assert tool_use_blocks, "no tool_use block in echoed assistant content"
        echoed_block = tool_use_blocks[0]
        assert isinstance(echoed_block["input"], dict), (
            f"echoed input must be a dict, got {type(echoed_block['input']).__name__!r}: "
            f"{echoed_block['input']!r}"
        )
        assert echoed_block["input"] == {"service": "r2d2"}

    def test_missing_id_synthesized_in_echo_and_pairs_tool_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A tool_use block with no ``id`` must get a synthesized id before
        the echo-back so the echoed assistant block and the paired tool_result
        in the user message reference the same id — keeping the protocol valid."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")

        captured_convos: list[list] = []

        def capturing_post(seat, messages, *, system, tools):  # type: ignore[misc]
            captured_convos.append([m for m in messages])
            call_n = len(captured_convos)
            if call_n == 1:
                return {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            # deliberately no "id" key
                            "name": "agent_list",
                            "input": {},
                        }
                    ],
                }
            return {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Synthesized id worked."}],
            }

        monkeypatch.setattr(cc, "_post_with_tools", capturing_post)
        seat = cc.SEATS["yoda"]
        answer = cc._tool_turn(seat, [{"role": "user", "content": "q"}])
        assert answer == "Synthesized id worked."
        assert len(captured_convos) == 2, "expected exactly 2 POSTs"
        second_convo = captured_convos[1]
        # Find the echoed assistant block
        assistant_msgs = [m for m in second_convo if m.get("role") == "assistant"]
        assert assistant_msgs, "no assistant message in second POST"
        asst_content = assistant_msgs[-1]["content"]
        assert isinstance(asst_content, list)
        tool_use_blocks = [b for b in asst_content if b.get("type") == "tool_use"]
        assert tool_use_blocks, "no tool_use block in echoed assistant content"
        echoed_id = tool_use_blocks[0].get("id")
        assert echoed_id, "echoed tool_use block must have a synthesized id"
        # The user message (tool_result) must pair the same id
        user_msgs = [m for m in second_convo if m.get("role") == "user"]
        assert user_msgs, "no user message in second POST"
        last_user = user_msgs[-1]
        user_content = last_user["content"]
        assert isinstance(user_content, list), "user content must be a list"
        tool_results = [b for b in user_content if b.get("type") == "tool_result"]
        assert tool_results, "no tool_result in user message"
        assert tool_results[0]["tool_use_id"] == echoed_id, (
            f"tool_result id {tool_results[0]['tool_use_id']!r} does not match "
            f"echoed id {echoed_id!r}"
        )

    def test_missing_name_only_no_empty_content_array_posted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A tool_use block with no ``name`` (but otherwise present id) must be
        dropped entirely from the echo-back.  If that leaves no tool_results to
        send, the assistant+user pair must NOT be appended and a second POST must
        NOT be made — an empty ``content: []`` POSTed as a user message is a
        protocol 400."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")

        post_calls: list[list] = []

        def capturing_post(seat, messages, *, system, tools):  # type: ignore[misc]
            post_calls.append(list(messages))
            return {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_noname",
                        # deliberately no "name" key
                        "input": {},
                    }
                ],
            }

        monkeypatch.setattr(cc, "_post_with_tools", capturing_post)
        seat = cc.SEATS["yoda"]
        # Only one POST should be made; the result must be the honest fallback string
        answer = cc._tool_turn(seat, [{"role": "user", "content": "q"}])
        assert len(post_calls) == 1, (
            f"expected exactly 1 POST (no looping on all-malformed batch), got {len(post_calls)}"
        )
        # Must not have posted an empty content array
        for convo in post_calls:
            for msg in convo:
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, list):
                        assert content, (
                            "empty user content list was included — protocol 400 territory"
                        )
        # Answer must be the honest fallback, not a crash
        assert isinstance(answer, str)
        assert answer  # not empty

    def test_malformed_block_loop_cannot_balloon(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Regression for the 2026-06-12 kernel panic: a model that returns a
        malformed-but-id'd tool_use block on EVERY call must NOT loop forever.

        The old loop skipped the cap and breaker for malformed blocks, so it
        appended to ``convo`` every pass and grew until the box OOM'd. The
        breaker (and the MAX_TOOL_LOOP_ITERATIONS backstop) must end the turn
        within a handful of POSTs. The fake raises past a safety ceiling so a
        future regression can't re-balloon the machine while this test runs.
        """
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        posts = {"n": 0}
        safety_ceiling = 100  # far above any legitimate bound; a tripwire, not a limit

        def forever_malformed(seat, messages, *, system, tools):  # type: ignore[misc]
            posts["n"] += 1
            if posts["n"] > safety_ceiling:
                raise AssertionError(
                    f"_tool_turn made {posts['n']} POSTs without terminating — "
                    "the unbounded-loop balloon has regressed"
                )
            return {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_loop",
                        "name": "logs_tail",
                        "input": "not valid json{",  # unparseable → recoverable malformation
                    }
                ],
            }

        monkeypatch.setattr(cc, "_post_with_tools", forever_malformed)
        answer = cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "loop forever"}])
        assert isinstance(answer, str) and answer
        assert posts["n"] <= cc.MAX_TOOL_LOOP_ITERATIONS, (
            f"loop must terminate within {cc.MAX_TOOL_LOOP_ITERATIONS} POSTs, made {posts['n']}"
        )
        # The error breaker should end it well before the hard backstop.
        assert answer == "(instrument errors — answering without further tools)"

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

    def test_run_tool_loop_accumulates_multiple_exchanges(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Exchanges accumulate across every tool call in the turn — the list
        is initialized once before the loop, not per iteration. The voice
        phase (Task 4) flattens ALL findings, so a regression that reset the
        list each pass would silently drop every finding but the last."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        monkeypatch.setattr(
            cc,
            "_post_with_tools",
            self._fake_responses(
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "agent_list", "input": {}}
                    ],
                },
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "logs_tail",
                            "input": {"service": "r2d2"},
                        }
                    ],
                },
                {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Both checked."}]},
            ),
        )
        result = cc._run_tool_loop(cc.SEATS["yoda"], [{"role": "user", "content": "check both"}])
        assert result.answer == "Both checked."
        assert [e.tool for e in result.exchanges] == ["agent_list", "logs_tail"]


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

    def test_tool_model_defaults_none_and_is_optional(self) -> None:
        # Unarmed seats carry no tool_model.
        assert cc.SEATS["windu"].tool_model is None
        # The field exists and is constructible.
        s = cc.Seat(label="X", model="m", persona="p", style="white", verb="thinks")
        assert s.tool_model is None
        s2 = cc.Seat(
            label="X",
            model="m",
            persona="p",
            style="white",
            verb="thinks",
            tool_model="gemini-31-pro",
        )
        assert s2.tool_model == "gemini-31-pro"


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

    @staticmethod
    def _fake_responses(*responses: dict) -> object:
        seq = iter(responses)

        def fake_post(seat, messages, *, system, tools):  # type: ignore[misc]
            return next(seq)

        return fake_post

    def test_ctrl_c_aborts_tool_turn_not_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KeyboardInterrupt from the armed (no-tool_model) loop must yield
        '(turn aborted)' in the transcript, not a propagated exception."""

        def raising(seat, messages):  # type: ignore[misc]
            raise KeyboardInterrupt

        monkeypatch.setattr(cc, "_tool_turn", raising)
        transcript = cc.Transcript()
        seat = replace(cc.SEATS["yoda"], tool_model=None)  # force the single-model path
        cc._say_turn(seat, transcript, "what is status?")
        assert transcript.messages()[-1]["content"] == "(turn aborted)"

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

    def test_streaming_delta_with_markup_completes_without_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deltas containing raw Rich markup like ``[/bad]`` and ``[red]x``
        must not raise MarkupError.  The transcript must hold the verbatim
        concatenated text (no exceptions propagated)."""
        seat = cc.SEATS["windu"]  # unarmed → streaming path

        def fake_stream(s, messages, *, system):  # type: ignore[misc]
            yield "[/bad]"
            yield "[red]x"
            yield " hello"

        monkeypatch.setattr(cc, "_stream", fake_stream)
        transcript = cc.Transcript()
        # Must complete without raising
        cc._say_turn(seat, transcript, "q")
        msgs = transcript.messages()
        assert msgs[-1]["role"] == "assistant"
        # The verbatim text must be in the transcript
        assert msgs[-1]["content"] == "[/bad][red]x hello"

    def test_streaming_error_with_markup_message_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception raised from the streaming path whose message contains
        Rich markup (e.g. ``[/bad]``) must be printed safely — the error
        handler itself must not raise a second MarkupError."""
        seat = cc.SEATS["windu"]  # unarmed → streaming path

        def exploding_stream(s, messages, *, system):  # type: ignore[misc]
            msg = "network error: [/bad] in response"
            raise RuntimeError(msg)
            yield  # make it a generator

        monkeypatch.setattr(cc, "_stream", exploding_stream)
        transcript = cc.Transcript()
        # Must complete without raising — the error is printed safely
        cc._say_turn(seat, transcript, "q")
        msgs = transcript.messages()
        # The error path adds a placeholder to the transcript
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == "(seat unavailable)"

    def test_gather_then_voice_opus_voices_gemini_findings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An armed seat with a tool_model gathers on the tool_model then
        voices on seat.model. The voice model must receive the flattened
        findings as TEXT (never tool_use/tool_result blocks) and its streamed
        text is the answer."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        seat = cc.Seat(
            label="Yoda",
            model="opus-voice",
            persona="P",
            style="green",
            verb="ponders",
            tools=("agent_list",),
            tool_model="gemini-hands",
        )
        monkeypatch.setattr(
            cc,
            "_post_with_tools",
            self._fake_responses(
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "agent_list", "input": {}}
                    ],
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
        assert seen["model"] == "opus-voice"
        blob = repr(seen["messages"])
        assert "tool_use" not in blob and "tool_result" not in blob
        assert "agent_list" in blob
        assert "Instruments you have" not in seen["system"]
        assert transcript.messages()[-1]["content"] == "Healthy, the agents are."

    def test_no_tool_turn_is_voice_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When the tool_model calls no tools, it's effectively voice-only:
        the voice model answers the bare question (no findings block)."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        seat = cc.Seat(
            label="Yoda",
            model="opus-voice",
            persona="P",
            style="green",
            verb="ponders",
            tools=("agent_list",),
            tool_model="gemini-hands",
        )
        monkeypatch.setattr(
            cc,
            "_post_with_tools",
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
        assert captured["messages"][-1]["content"] == "how is it?"
        assert transcript.messages()[-1]["content"] == "Quiet, the haus is."

    def test_voice_failure_falls_back_to_gathered_answer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If the voice model errors, return the tool_model's own final text
        rather than nothing."""
        monkeypatch.setattr(cc.council_tools, "AUDIT_LEDGER", tmp_path / "a.jsonl")
        seat = cc.Seat(
            label="Yoda",
            model="opus-voice",
            persona="P",
            style="green",
            verb="ponders",
            tools=("agent_list",),
            tool_model="gemini-hands",
        )
        monkeypatch.setattr(
            cc,
            "_post_with_tools",
            self._fake_responses(
                {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "gathered fallback"}],
                },
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
            label="Yoda",
            model="opus-voice",
            persona="P",
            style="green",
            verb="ponders",
            tools=("agent_list",),
            tool_model="gemini-hands",
        )

        def reject(seat_, messages, *, system, tools):  # type: ignore[misc]
            raise cc.ToolsRejected("HTTP 400")

        monkeypatch.setattr(cc, "_post_with_tools", reject)

        def fake_stream(s, messages, *, system):  # type: ignore[misc]
            assert "Instruments you have" not in system
            yield "Chat only, this turn."

        monkeypatch.setattr(cc, "_stream", fake_stream)
        transcript = cc.Transcript()
        cc._say_turn(seat, transcript, "q")
        assert transcript.messages()[-1]["content"] == "Chat only, this turn."


class TestCanonVoice:
    def test_yoda_persona_carries_the_may31_canon(self) -> None:
        p = cc.SEATS["yoda"].persona
        assert "invert" in p.lower(), "movie-voice inversion is the default register"
        assert "plain" in p.lower() and "tool" in p.lower(), (
            "the machine-boundary line is load-bearing now that he has tools"
        )
        assert "NO tools" not in p, "the tool clause is composed by _persona(), not hardcoded"


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
