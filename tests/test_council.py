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

import pytest

from sanctum_cli.commands import council as cc


class TestSeats:
    def test_roster_shape_and_doctrine(self) -> None:
        assert set(cc.SEATS) == {"yoda", "windu", "quigon", "mundi", "cilghal", "jocasta"}
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
    def test_council_ask_collects_every_seat_and_synthesizes(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
