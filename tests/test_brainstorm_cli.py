"""Tests for `sanctum brainstorm` — neurodiversity enforcement + voice preservation.

The family expectations here are transcribed INDEPENDENTLY from the doctrine (not
imported from the production `_FAMILY_BY_MODEL`), so a regression in the production
map is caught by divergence rather than silently agreed with.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import brainstorm as bs
from sanctum_cli.commands.brainstorm import SeatResult, Status

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()

# Independent oracle (NOT bs._FAMILY_BY_MODEL).
_DOCTRINE_FAMILY = {
    "Yoda": "claude", "Mundi": "claude", "Qui-Gon": "codestral",
    "Cilghal": "qwen", "Windu": "gemini",
}


def _ok(seat: str, family: str | None = None, content: str = "idea") -> SeatResult:
    fam = family or _DOCTRINE_FAMILY[seat]
    model = bs.SEATS[seat]["model"]
    return SeatResult(seat, model, model, content, None, fam, Status.OK, False, None)


def _degraded(seat: str) -> SeatResult:
    return SeatResult(seat, bs.SEATS[seat]["model"], "council-mlx", "fallback answer", None,
                      "qwen", Status.DEGRADED, True, _DOCTRINE_FAMILY[seat])


def _absent(seat: str, error: str = "boom") -> SeatResult:
    return SeatResult(seat, bs.SEATS[seat]["model"], None, None, error,
                      "absent", Status.ABSENT, True, _DOCTRINE_FAMILY[seat])


def _ask_returning(table: dict[str, SeatResult]):
    def _fake(_client, seat, _model, _lens, _topic, _max_tokens, _deadline):
        return table[seat]
    return _fake


# ───────────────────────── unit: family resolution ─────────────────────────
def test_family_of_known_aliases() -> None:
    assert bs._family_of("council-max-thinking") == "claude"
    assert bs._family_of("council-code") == "codestral"
    assert bs._family_of("council-mlx") == "qwen"
    for g in ("gemini-31-pro", "gemini-3-pro", "gemini-25-pro"):
        assert bs._family_of(g) == "gemini"


def test_family_of_unknown_and_none() -> None:
    assert bs._family_of("experimental-xyz") == "unknown:experimental-xyz"
    assert bs._family_of(None) == "absent"
    assert bs._real_family("claude") is True
    assert bs._real_family("unknown:experimental-xyz") is False
    assert bs._real_family("absent") is False


def test_fallback_family_is_qwen_and_seats_tagged() -> None:
    assert bs.FALLBACK_FAMILY == "qwen"
    # structural ceiling: Yoda + Mundi are BOTH claude (4 families across 5 seats)
    assert bs.SEATS["Yoda"]["family"] == bs.SEATS["Mundi"]["family"] == "claude"


# ───────────────────────── unit: diversity accounting ─────────────────────────
def test_assess_diversity_healthy_dedups_claude() -> None:
    chosen = list(bs.SEATS)
    results = [_ok(s) for s in chosen]
    div = bs._assess_diversity(results, chosen)
    # 5 seats, but claude appears ONCE -> 4 families, never 5
    assert div.designed == frozenset({"claude", "codestral", "qwen", "gemini"})
    assert div.achieved == frozenset({"claude", "codestral", "qwen", "gemini"})
    assert div.answered_seats == 5
    assert div.redundant == {"claude": ["Yoda", "Mundi"]}


def test_assess_diversity_gemini_absent_marks_lost() -> None:
    chosen = list(bs.SEATS)
    results = [_ok(s) for s in chosen if s != "Windu"] + [_absent("Windu")]
    div = bs._assess_diversity(results, chosen)
    assert "gemini" not in div.achieved          # the voice is gone
    assert "Windu" in div.absent_seats
    assert div.achieved == frozenset({"claude", "codestral", "qwen"})


def test_assess_diversity_unknown_models_do_not_inflate() -> None:
    # must-fix 1: two seats answering on unrecognized models must NOT count as 2 families
    chosen = ["Yoda", "Windu"]
    results = [
        SeatResult("Yoda", "exp-a", "exp-a", "x", None, "unknown:exp-a", Status.OK, False, None),
        SeatResult("Windu", "exp-b", "exp-b", "x", None, "unknown:exp-b", Status.OK, False, None),
    ]
    div = bs._assess_diversity(results, chosen)
    assert div.achieved == frozenset()           # neither unknown counts


def test_assess_diversity_degraded_qwen_not_fresh_voice() -> None:
    chosen = list(bs.SEATS)
    results = [_ok(s) for s in chosen if s != "Windu"] + [_degraded("Windu")]
    div = bs._assess_diversity(results, chosen)
    assert "gemini" not in div.achieved
    assert div.degraded_families == frozenset({"qwen"})  # fallback adds only qwen, already present


# ───────────────────────── unit: _ask voice preservation ─────────────────────────
class _FakeResp:
    def __init__(self, status: int = 200, payload: dict | None = None, headers: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError("e", request=req, response=httpx.Response(self.status_code, headers=self.headers))

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, handler) -> None:
        self._h = handler

    def post(self, _path, json=None, timeout=None):
        return self._h(json)


def _deadline() -> float:
    import time
    return time.monotonic() + 100


def test_ask_gemini_starvation_escalates_same_model_no_fallback() -> None:
    calls: list[int] = []

    def handler(body: dict) -> _FakeResp:
        calls.append(body["max_tokens"])
        if body["model"] != "gemini-31-pro":
            raise AssertionError("must NOT touch the fallback before escalation")
        if body["max_tokens"] < bs.THINKING_BUDGET_ESCALATED:
            # starved: empty content but reasoning tokens spent
            return _FakeResp(payload={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                                      "usage": {"completion_tokens_details": {"reasoning_tokens": 60}}})
        return _FakeResp(payload={"choices": [{"message": {"content": "PONG"}, "finish_reason": "stop"}]})

    with patch.object(bs, "_sleep"):
        r = bs._ask(_FakeClient(handler), "Windu", "gemini-31-pro", "lens", "topic", 900, _deadline())
    assert r.status is Status.OK
    assert r.family == "gemini"
    assert r.model_used == "gemini-31-pro"
    assert max(calls) >= bs.THINKING_BUDGET_ESCALATED


def test_ask_429_one_bounded_retry_then_recovers() -> None:
    state = {"n": 0}

    def handler(body: dict) -> _FakeResp:
        state["n"] += 1
        if state["n"] == 1:
            return _FakeResp(status=429, headers={"Retry-After": "99"})
        return _FakeResp(payload={"choices": [{"message": {"content": "ok"}}]})

    with patch.object(bs, "_sleep") as slept:
        r = bs._ask(_FakeClient(handler), "Windu", "gemini-31-pro", "lens", "topic", 900, _deadline())
    assert r.status is Status.OK
    slept.assert_called()
    assert slept.call_args.args[0] <= bs.RETRY_BACKOFF_CAP_S  # never the full 45s window


def test_ask_never_raises_on_internal_error() -> None:
    # must-fix 3: a crashing client must yield ABSENT, never propagate
    def handler(_body: dict):
        raise RuntimeError("usage payload exploded")

    r = bs._ask(_FakeClient(handler), "Cilghal", "council-mlx", "lens", "topic", 900, _deadline())
    assert r.status is Status.ABSENT
    assert "usage payload exploded" in (r.error or "")


# ───────────────────────── CLI: end-to-end via patched _ask ─────────────────────────
def test_cli_healthy_all_families(full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {s: _ok(s) for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "topic"])
    assert result.exit_code == 0, result.stdout
    combined = result.stdout + (result.stderr or "")
    assert "homogenized" not in combined.lower()
    assert "5/5 seats answered" in combined


def test_cli_redundant_claude_badge(full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {s: _ok(s) for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "topic"])
    assert result.exit_code == 0
    assert "redundant" in result.stdout.lower()  # Yoda+Mundi both claude


def test_cli_gemini_degraded_badge(full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {s: (_degraded(s) if s == "Windu" else _ok(s)) for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "--json", "topic"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    windu = next(r for r in payload["seats"] if r["seat"] == "Windu")
    assert windu["status"] == "degraded"
    assert windu["family"] == "qwen"
    assert windu["fallback_from"] == "gemini"
    assert "gemini" not in payload["diversity"]["achieved_families"]


def test_cli_lost_family_warns_even_when_floor_met(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # must-fix 2: gemini ABSENT, others OK -> achieved {claude,codestral,qwen}=3 == floor 3,
    # diversity_ok True, but the lost gemini voice MUST still surface a notice.
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {s: (_absent(s) if s == "Windu" else _ok(s)) for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "topic"])
    assert result.exit_code == 0  # 4 others answered, floor met
    combined = result.stdout + (result.stderr or "")
    assert "lost" in combined.lower() and "gemini" in combined.lower()


def test_cli_lenient_collapse_warns_exit_zero(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    # everyone collapses to qwen (Cilghal native, others degrade)
    table = {s: (_ok("Cilghal") if s == "Cilghal" else _degraded(s)) for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "topic"])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "homogenized" in combined.lower()


def test_cli_strict_collapse_exit_two(full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {s: (_ok("Cilghal") if s == "Cilghal" else _degraded(s)) for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "--strict", "topic"])
    assert result.exit_code == 2  # PROVIDER_ERROR


def test_cli_strict_json_emits_block_then_exits_two(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {s: (_ok("Cilghal") if s == "Cilghal" else _degraded(s)) for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "--json", "--strict", "topic"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)  # stdout is still pure JSON
    assert payload["diversity"]["ok"] is False


def test_cli_subset_floor_does_not_false_trip(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {"Yoda": _ok("Yoda"), "Windu": _ok("Windu")}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "-s", "Yoda,Windu", "--strict", "topic"])
    assert result.exit_code == 0  # designed 2 -> floor min(3,2)=2, met


def test_cli_unknown_seat_user_error(full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    result = runner.invoke(app, ["brainstorm", "-s", "Vader", "topic"])
    assert result.exit_code == 1
    assert "unknown seat" in (result.stdout + (result.stderr or "")).lower()


def test_cli_empty_topic_user_error(full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    result = runner.invoke(app, ["brainstorm"], input="")
    assert result.exit_code == 1
    assert "topic" in (result.stdout + (result.stderr or "")).lower()


def test_cli_all_absent_provider_error(full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {s: _absent(s, "empty response") for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "topic"])
    assert result.exit_code == 2  # no seat responded -> PROVIDER_ERROR


def test_cli_all_unreachable_network_error(full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {s: _absent(s, "ConnectError: [Errno 61] Connection refused") for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "topic"])
    assert result.exit_code == 3  # NETWORK_ERROR


def test_cli_json_schema_pins_diversity_block(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    table = {s: _ok(s) for s in bs.SEATS}
    with patch.object(bs, "_ask", _ask_returning(table)):
        result = runner.invoke(app, ["brainstorm", "--json", "topic"])
    payload = json.loads(result.stdout)
    for k in ("designed_families", "achieved_families", "degraded_families", "effective_families",
              "floor", "ok", "strict", "answered_seats", "total_seats",
              "absent_seats", "degraded_seats", "redundant_families", "lost_designed_families"):
        assert k in payload["diversity"], k
    for row in payload["seats"]:
        for k in ("seat", "model_attempted", "model_used", "family", "status", "degraded",
                  "fallback_from", "response", "error"):
            assert k in row, k
        assert row["status"] in ("ok", "degraded", "absent")


def test_e2e_real_boundary_gemini_starve_then_escalate(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Headline real-boundary test: NO _ask patch. Drives the actual _summon + _ask +
    # JSON parse + starvation->escalation + diversity through an httpx.MockTransport,
    # exercising the integration the unit tests cannot (the max_tokens/timeout swap
    # bug lived exactly here and was invisible to the _ask-patched CLI tests).
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    calls: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model, toks = body["model"], body["max_tokens"]
        calls.append((model, toks))
        if model == "gemini-31-pro" and toks < bs.THINKING_BUDGET_ESCALATED:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 60}},
            })
        return httpx.Response(200, json={"choices": [{"message": {"content": f"{model} answer"}}]})

    real_client = bs.httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    with patch.object(bs.httpx, "Client", factory), patch.object(bs, "_sleep"):
        result = runner.invoke(app, ["brainstorm", "--json", "--url", "http://127.0.0.1:4040", "topic"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    windu = next(r for r in payload["seats"] if r["seat"] == "Windu")
    # recovered on its OWN gemini model via escalation — never fell back to qwen
    assert windu["status"] == "ok"
    assert windu["family"] == "gemini"
    assert any(m == "gemini-31-pro" and t >= bs.THINKING_BUDGET_ESCALATED for m, t in calls)
    assert "gemini" in payload["diversity"]["achieved_families"]
    assert payload["diversity"]["achieved_families"] == ["claude", "codestral", "gemini", "qwen"]
