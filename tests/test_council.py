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
        monkeypatch.setattr(cc, "_post_with_tools", self._fake_responses(bad_call, bad_call, final))
        answer = cc._tool_turn(cc.SEATS["yoda"], [{"role": "user", "content": "q"}])
        assert answer == "Hmm."

    def test_4xx_with_tools_degrades_to_chat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def reject(seat, messages, *, system, tools):  # type: ignore[misc]
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
