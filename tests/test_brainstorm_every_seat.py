"""`sanctum brainstorm` must let EVERY seat answer — or say exactly why it could not.

Regression suite for the 2026-09-19 council incident (DF2 q12-q14): every Yoda
attempt and most Cilghal attempts died to the CLIENT's own per-seat clock
(150 s / 90 s) while the backends were still working; tunnel restarts surfaced as
"check proxyd seat routing"; answers cut at the 600-token cap and answers served
by other models (council-mlx, a HOSTED qwen36-plus, the non-ablated
council-local-think) were all counted as healthy voices.

Everything here runs against a FAKE proxyd (httpx.MockTransport) and, where time
matters, a virtual clock: a slow seat, a streaming seat, a disconnecting seat, a
truncated seat, an empty seat, a diverted seat. No network, no models.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from sanctum_cli.cli import app, brainstorm_top
from sanctum_cli.commands import brainstorm as bs
from sanctum_cli.errors import ProviderError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_real_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """`full_instance_yaml` enables telemetry with no path, which resolves to the REAL
    ~/.sanctum/telemetry/cli.jsonl — every CLI test would append a line there."""
    monkeypatch.setattr(bs, "_load_telemetry", lambda: None)


# ───────────────────────── the fake proxyd ─────────────────────────
class Clock:
    """Virtual monotonic clock; stands in for the `time` module inside brainstorm."""

    def __init__(self) -> None:
        self.t = 1000.0
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        with self._lock:
            self.t += s

    def sleep(self, s: float) -> None:
        self.advance(s)


class FakeProxyd:
    """Routes POST /v1/chat/completions by requested model to a script of steps (one
    per request; the last step repeats) and answers GET /health."""

    def __init__(self, script: dict[str, list[Any]], clock: Clock | None = None,
                 seats_health: dict[str, Any] | None = None, health_down: int = 0,
                 hold_s: float = 0.0) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.clock = clock
        self.seats_health = seats_health or {}
        self.health_down = health_down
        self.hold_s = hold_s          # REAL seconds each POST stays in flight (scheduling tests)
        self.calls: list[dict[str, Any]] = []
        self.health_calls = 0
        self._lock = threading.Lock()
        self.inflight: dict[str, int] = {}
        self.max_inflight: dict[str, int] = {}
        self.total_inflight = 0
        self.max_total_inflight = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            with self._lock:
                self.health_calls += 1
                down = self.health_down > 0
                if down:
                    self.health_down -= 1
            if down:
                raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
            return httpx.Response(200, json={"status": "healthy", "seats": self.seats_health})
        body = json.loads(request.content)
        model = body["model"]
        with self._lock:
            steps = self.script[model]
            step = steps.pop(0) if len(steps) > 1 else steps[0]
            self.calls.append({"model": model, "max_tokens": body["max_tokens"],
                               "stream": bool(body.get("stream")),
                               "no_fallback": request.headers.get("x-sanctum-no-fallback"),
                               "timeout": dict(request.extensions.get("timeout") or {})})
        lane = bs._lane_of(model)
        with self._lock:
            self.inflight[lane] = self.inflight.get(lane, 0) + 1
            self.max_inflight[lane] = max(self.max_inflight.get(lane, 0), self.inflight[lane])
            self.total_inflight += 1
            self.max_total_inflight = max(self.max_total_inflight, self.total_inflight)
        try:
            if self.hold_s:
                time.sleep(self.hold_s)
            return step(self, request, body)
        finally:
            with self._lock:
                self.inflight[lane] -= 1
                self.total_inflight -= 1

    def models_called(self) -> list[str]:
        return [c["model"] for c in self.calls]


def answer(content: str = "a complete answer", *, finish: str | None = "stop",
           served: str | None = None, latency: float = 1.0,
           headers: dict[str, str] | None = None) -> Any:
    """A non-streaming seat that answers after `latency` seconds — or, when that is
    longer than the client's read timeout, lets the client's clock run out."""
    def step(p: FakeProxyd, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        read_to = float((request.extensions.get("timeout") or {}).get("read") or 1e9)
        if latency > read_to:
            if p.clock:
                p.clock.advance(read_to)
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        if p.clock:
            p.clock.advance(latency)
        payload: dict[str, Any] = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
        if served:
            payload["model"] = served
        return httpx.Response(200, json=payload, headers=headers)
    return step


def disconnect(*, tunnel_down: int = 0, after: float = 0.0) -> Any:
    """The connection dies mid-request. ``tunnel_down`` = how many /health probes are
    refused afterwards: >0 is a tunnel restart (the ssh listener is gone for a while),
    0 is proxyd itself closing the response while /health still answers."""
    def step(p: FakeProxyd, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if p.clock and after:
            p.clock.advance(after)
        with p._lock:
            p.health_down = tunnel_down
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
    return step


def http(code: int, message: str = "fake", headers: dict[str, str] | None = None) -> Any:
    def step(p: FakeProxyd, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(code, json={"error": {"message": message}}, headers=headers)
    return step


def sse(deltas: list[str], *, served: str = "claude-fable-5", keepalives: int = 0,
        tick: float = 15.0, finish: str | None = "stop", done: bool = True,
        headers: dict[str, str] | None = None) -> Any:
    """A streaming seat: role chunk, `keepalives` empty deltas `tick` s apart (the
    cathedral's prefill keep-alive), content deltas, finish chunk, [DONE]."""
    def step(p: FakeProxyd, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        assert body.get("stream") is True, "a streaming seat must be asked to stream"

        def chunk(delta: dict[str, Any], fin: str | None = None) -> bytes:
            obj = {"object": "chat.completion.chunk", "model": served,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
            return f"data: {json.dumps(obj)}\n\n".encode()

        def gen() -> Iterator[bytes]:
            yield b":ok\n\n"
            yield chunk({"role": "assistant", "content": ""})
            for _ in range(keepalives):
                if p.clock:
                    p.clock.advance(tick)
                yield chunk({"content": ""})
            for d in deltas:
                yield chunk({"content": d})
            if finish:
                yield chunk({}, finish)
            if done:
                yield b"data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream", **(headers or {})},
                              content=gen())
    return step


def run_cli(args: list[str], proxy: FakeProxyd, monkeypatch: pytest.MonkeyPatch,
            instance: Path, clock: Clock | None = None) -> Any:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(instance))
    real_client = httpx.Client

    def factory(*a: Any, **kw: Any) -> httpx.Client:
        kw.pop("verify", None)
        kw["transport"] = httpx.MockTransport(proxy.handle)
        return real_client(*a, **kw)

    with ExitStack() as st:
        st.enter_context(patch.object(bs.httpx, "Client", factory))
        if clock is not None:
            st.enter_context(patch.object(bs, "time", clock))
            st.enter_context(patch.object(bs, "_sleep", clock.sleep))
        else:
            st.enter_context(patch.object(bs, "_sleep", lambda _s: None))
        return runner.invoke(app, ["brainstorm", "--json", "--url", "http://127.0.0.1:4040", *args])


def seat(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(r for r in payload["seats"] if r["seat"] == name)


# ───────────────────────── budgets ─────────────────────────
def test_bridge_budget_outlasts_the_bridge_kill() -> None:
    # The Max bridge kills a claude CLI run at 300 s; a client that quits first can
    # never see the answer (09-19: every Yoda attempt ended at exactly 150 s).
    assert bs._seat_budget("Yoda", 240) > 300
    assert bs._seat_budget("Jocasta", 240) > 300


def test_seat_timeout_can_raise_every_lane_including_the_bridge() -> None:
    assert bs._seat_budget("Yoda", 240, 900) == 900
    assert bs._seat_budget("Cilghal", 240, 900) == 900   # was pinned at 90 s, unraisable
    assert bs._seat_budget("Mundi", 240, 45) == 45        # and can lower


def test_timeout_is_a_ceiling_and_zero_removes_it() -> None:
    assert bs._seat_budget("Qui-Gon", 90) == 90
    assert bs._seat_budget("Qui-Gon", 0) == bs.LANE_TIMEOUTS["code"]
    assert bs._seat_budget("Yoda", 90) == bs.LANE_TIMEOUTS["bridge"]   # bridge exempt, as before


def test_lane_budgets_are_env_overridable() -> None:
    lanes = bs._lane_timeouts({"SANCTUM_COUNCIL_TIMEOUT_GROK": "400",
                               "SANCTUM_COUNCIL_TIMEOUT_CODE": "junk"})
    assert lanes["grok"] == 400.0
    assert lanes["code"] == bs._LANE_TIMEOUT_DEFAULTS["code"]   # bad value ignored, not a crash


def test_slow_bridge_seat_answers_inside_its_lane_budget(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Yoda takes 200 s — longer than the old 150 s client clock, shorter than the bridge's.
    clock = Clock()
    proxy = FakeProxyd({"council-max-thinking": [answer("wisdom", served="claude-fable-5", latency=200)]},
                       clock=clock)
    res = run_cli(["-s", "Yoda", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 0, res.stdout + res.stderr
    yoda = seat(json.loads(res.stdout), "Yoda")
    assert yoda["outcome"] == "answered" and yoda["response"] == "wisdom"
    assert yoda["served_model"] == "claude-fable-5" and yoda["provenance"] == "match"
    t = proxy.calls[0]["timeout"]
    assert t["read"] > 300 and t["connect"] == bs.CONNECT_TIMEOUT_S   # split, not one scalar


def test_seat_past_its_budget_is_a_client_timeout_asked_once(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    proxy = FakeProxyd({"council-max-thinking": [answer(latency=10_000)],
                        "council-mlx": [answer("qwen")]}, clock=clock)
    res = run_cli(["-s", "Yoda", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 2
    yoda = seat(json.loads(res.stdout), "Yoda")   # JSON is printed even when nobody answered
    assert yoda["outcome"] == "timeout" and yoda["response"] is None
    assert "client budget expired" in yoda["error"]
    # a timeout is never re-fired (the server is still working) and leaves no fallback time
    assert proxy.models_called() == ["council-max-thinking"]
    assert "seat routing" not in res.stderr and "--seat-timeout" in res.stderr


def test_local_lane_timeout_is_not_retried(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    proxy = FakeProxyd({"council-heretic": [answer(latency=10_000)],
                        "council-mlx": [answer("qwen")]}, clock=clock)
    res = run_cli(["-s", "Cilghal", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 2
    assert proxy.models_called() == ["council-heretic"]


# ───────────────────────── transport drops ─────────────────────────
def test_disconnect_waits_for_health_then_retries_once(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The MBP watchdog kickstarts the tunnel: the in-flight request dies, /health is
    # refused for a while, then comes back. The seat must answer on the retry.
    clock = Clock()
    # the tunnel goes down at the moment of the drop: the next 2 health probes are refused
    proxy = FakeProxyd({"council-finance": [disconnect(tunnel_down=2), answer("numbers", served="grok-4.6")]},
                       clock=clock)
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 0, res.stdout + res.stderr
    mundi = seat(json.loads(res.stdout), "Mundi")
    assert mundi["outcome"] == "answered" and mundi["attempts"] == 2
    assert proxy.models_called() == ["council-finance", "council-finance"]
    assert proxy.health_calls >= 3   # pre-flight + 2 refused + 1 answered


class _DropThenAnswer:
    """Bare client double (no MockTransport): first POST dies mid-flight with the tunnel
    (the first /health probe is refused), then /health answers again."""

    def __init__(self) -> None:
        self.posts = 0
        self.gets = 0
        self.sent_headers: list[dict[str, str]] = []

    def post(self, _path: str, json: Any = None, timeout: Any = None,
             headers: Any = None) -> httpx.Response:
        self.posts += 1
        self.sent_headers.append(dict(headers or {}))
        req = httpx.Request("POST", "http://x/v1/chat/completions")
        if self.posts == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=req)
        return httpx.Response(200, json={"model": "grok-4.6", "choices": [
            {"message": {"content": "after the drop"}, "finish_reason": "stop"}]}, request=req)

    def get(self, _path: str, timeout: Any = None) -> httpx.Response:
        self.gets += 1
        req = httpx.Request("GET", "http://x/health")
        if self.gets == 1:
            raise httpx.ConnectError("[Errno 61] Connection refused", request=req)
        return httpx.Response(200, json={"seats": {}}, request=req)


def test_ask_default_options_retry_a_drop_once() -> None:
    client = _DropThenAnswer()
    with patch.object(bs, "_sleep"):
        r = bs._ask(client, "Mundi", "council-finance", "lens", "topic", 600,  # type: ignore[arg-type]
                    time.monotonic() + 100)
    assert r.content == "after the drop" and bs._outcome_of(r) is bs.Outcome.ANSWERED
    assert client.posts == 2 and client.gets >= 2


def test_persistent_disconnect_is_dropped_bounded_and_a_network_error(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    proxy = FakeProxyd({"council-finance": [disconnect(tunnel_down=1)],
                        "council-mlx": [disconnect(tunnel_down=1)]}, clock=clock)
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 3   # NETWORK_ERROR, not "check seat routing"
    mundi = seat(json.loads(res.stdout), "Mundi")
    assert mundi["outcome"] == "dropped"
    assert "severed" in mundi["error"]
    # one retry, never more — and no fallback over the same severed connection
    assert proxy.models_called() == ["council-finance", "council-finance"]


# ───────────────────────── streaming ─────────────────────────
def test_streaming_seat_assembles_the_answer_through_keepalives(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 20 keep-alives x 15 s = 300 s of queue/prefill before the first token.
    clock = Clock()
    proxy = FakeProxyd({"council-max-thinking": [sse(["Look ", "deeper."], keepalives=20)]}, clock=clock)
    res = run_cli(["-s", "Yoda", "--stream", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 0, res.stdout + res.stderr
    yoda = seat(json.loads(res.stdout), "Yoda")
    assert yoda["response"] == "Look deeper." and yoda["outcome"] == "answered"
    assert yoda["served_model"] == "claude-fable-5" and yoda["finish_reason"] == "stop"
    assert proxy.calls[0]["stream"] is True


def test_streaming_budget_is_enforced_between_keepalives(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stream of keep-alives never goes idle, so only the client's wall clock can end it.
    clock = Clock()
    proxy = FakeProxyd({"council-max-thinking": [sse(["late"], keepalives=100)]}, clock=clock)
    res = run_cli(["-s", "Yoda", "--stream", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 2
    yoda = seat(json.loads(res.stdout), "Yoda")
    assert yoda["outcome"] == "timeout" and yoda["response"] is None
    assert len(proxy.calls) == 1


def test_stream_that_ends_without_finishing_is_not_an_answer(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A clean close with no finish_reason and no [DONE] = the backend stopped mid-answer
    # (e.g. the heretic's DFlash2 refusal on the stream path). Not an answer, and not a
    # tunnel drop: the seat's own model is asked once, never re-fired.
    clock = Clock()
    cut = sse(["half an ans"], finish=None, done=False)
    proxy = FakeProxyd({"council-finance": [cut], "council-mlx": [cut]}, clock=clock)
    res = run_cli(["-s", "Mundi", "--stream", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    mundi = seat(json.loads(res.stdout), "Mundi")
    assert mundi["response"] is None and mundi["outcome"] == "error"
    assert mundi["partial_response"] == "half an ans"
    assert "stopped mid-answer" in mundi["error"]
    assert proxy.models_called().count("council-finance") == 1


def test_stream_severed_mid_answer_is_a_drop_and_retried(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tunnel restart mid-stream surfaces as a transport error, not a clean close.
    def severed(p: FakeProxyd, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        def gen() -> Iterator[bytes]:
            yield b'data: {"model": "grok-4.6", "choices": [{"delta": {"content": "par"}}]}\n\n'
            with p._lock:
                p.health_down = 1          # the ssh tunnel is gone for a moment
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())

    proxy = FakeProxyd({"council-finance": [severed, sse(["whole"], served="grok-4.6")]})
    res = run_cli(["-s", "Mundi", "--stream", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    mundi = seat(json.loads(res.stdout), "Mundi")
    assert mundi["response"] == "whole" and mundi["attempts"] == 2


def test_stream_request_answered_with_json_is_parsed(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # proxyd's gap guard passes a JSON body through to a stream request.
    proxy = FakeProxyd({"council-finance": [answer("json body", served="grok-4.6")]})
    res = run_cli(["-s", "Mundi", "--stream", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert seat(json.loads(res.stdout), "Mundi")["response"] == "json body"


# ───────────────────────── completeness ─────────────────────────
def test_truncated_seat_is_its_own_status_never_an_answer(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Qui-Gon's lane is single-lane: no automatic re-generation there.
    proxy = FakeProxyd({"council-code": [answer("cut mid-sen", finish="length",
                                                served="Muse-Glimmer-30B-4bit")],
                        "council-mlx": [answer("qwen")]})
    res = run_cli(["-s", "Qui-Gon", "-t", "600", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2
    payload = json.loads(res.stdout)
    qg = seat(payload, "Qui-Gon")
    assert qg["outcome"] == "truncated" and qg["response"] is None and qg["status"] == "absent"
    assert qg["partial_response"] == "cut mid-sen" and qg["finish_reason"] == "length"
    assert payload["diversity"]["truncated_seats"] == ["Qui-Gon"]
    assert proxy.models_called() == ["council-code"]   # no re-run, no qwen stand-in


def test_truncated_remote_seat_retries_once_with_room_to_finish(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-finance": [answer("(c) single-use across *all*", finish="length"),
                                            answer("the whole answer", served="grok-4.6")]})
    res = run_cli(["-s", "Mundi", "-t", "600", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert seat(json.loads(res.stdout), "Mundi")["response"] == "the whole answer"
    assert [c["max_tokens"] for c in proxy.calls] == [600, 1200]


def test_empty_seat_is_its_own_status(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-finance": [answer("", finish="stop")],
                        "council-mlx": [answer("", finish="stop")]})
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    mundi = seat(json.loads(res.stdout), "Mundi")
    assert mundi["outcome"] == "empty" and mundi["response"] is None


def test_failed_fallback_error_is_reported_alongside_the_home_error(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-finance": [http(500)], "council-mlx": [http(500)]})
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    err = seat(json.loads(res.stdout), "Mundi")["error"]
    assert "HTTP 500" in err and "fallback council-mlx: HTTP 500" in err


# ───────────────────────── provenance ─────────────────────────
def test_hosted_model_answering_a_local_seat_is_diverted_not_counted(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 09-19 q14b: Qui-Gon's "codestral ok" answer was qwen36-plus on OpenRouter.
    proxy = FakeProxyd({"council-code": [answer("hosted words", served="qwen/qwen3.6-plus")]})
    res = run_cli(["-s", "Qui-Gon", "topic"], proxy, monkeypatch, full_instance_yaml)
    payload = json.loads(res.stdout)
    qg = seat(payload, "Qui-Gon")
    assert qg["status"] == "degraded" and qg["outcome"] == "diverted"
    assert qg["model_used"] == "qwen/qwen3.6-plus" and qg["fallback_from"] == "codestral"
    assert "HOSTED" in qg["note"]
    assert "codestral" not in payload["diversity"]["achieved_families"]
    assert payload["diversity"]["diverted_seats"] == ["Qui-Gon"]


def test_own_models_as_reported_by_their_backends_are_answers() -> None:
    for seat_name, served in (("Yoda", "claude-fable-5"), ("Qui-Gon", "Muse-Glimmer-30B-4bit"),
                              ("Mundi", "grok-4.6"), ("Windu", "gemini-3.7-flash-high")):
        model = bs.SEATS[seat_name]["model"]
        verdict, _ = bs._provenance(model, bs.SEATS[seat_name]["family"], bs._lane_of(model), served, {})
        assert verdict == "match", (seat_name, served)


def test_heretic_shared_weights_are_settled_by_proxyd_health() -> None:
    # :6669 (heretic) and :1337 (council-local-think) are one process with one resident
    # id; only proxyd /health can say the heretic seat itself is failing.
    unhealthy = {"council-heretic": {"healthy": False, "error_rate_pct": 100}}
    healthy = {"council-heretic": {"healthy": True, "error_rate_pct": 0}}
    v1, note = bs._provenance("council-heretic", "heretic", "cathedral", "Qwen3.8-27B-4bit", unhealthy)
    v2, _ = bs._provenance("council-heretic", "heretic", "cathedral", "Qwen3.8-27B-4bit", healthy)
    assert v1 == "diverted" and "100%" in (note or "")
    assert v2 == "ambiguous"
    v3, _ = bs._provenance("council-heretic", "heretic", "cathedral",
                           "Qwen3.8-27B-4bit-champion-ablated", unhealthy)
    assert v3 == "match"


def test_cilghal_answer_while_heretic_is_down_is_diverted_end_to_end(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-heretic": [answer("plain qwen", served="Qwen3.8-27B-4bit")]},
                       seats_health={"council-heretic": {"healthy": False, "error_rate_pct": 100}})
    res = run_cli(["-s", "Cilghal", "topic"], proxy, monkeypatch, full_instance_yaml)
    cil = seat(json.loads(res.stdout), "Cilghal")
    assert cil["outcome"] == "diverted" and cil["family"] == "qwen"
    assert "unhealthy" in res.stderr


# ───────────────────────── every seat answers ─────────────────────────
def test_require_all_exits_nonzero_naming_each_missing_seat(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-max-thinking": [answer("ok", served="claude-fable-5")],
                        "council-finance": [http(500)], "council-mlx": [http(500)]})
    res = run_cli(["-s", "Yoda,Mundi", "--require-all", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2
    assert "Mundi=http_error" in res.stderr
    payload = json.loads(res.stdout)
    assert payload["diversity"]["all_answered"] is False
    assert payload["diversity"]["own_voice_seats"] == ["Yoda"]


def test_require_all_passes_when_every_seat_answers(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-max-thinking": [answer("a", served="claude-fable-5")],
                        "council-finance": [answer("b", served="grok-4.6")]})
    res = run_cli(["-s", "Yoda,Mundi", "--require-all", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert json.loads(res.stdout)["diversity"]["all_answered"] is True


def test_repoll_reasks_only_the_seats_that_did_not_answer(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({
        "council-max-thinking": [answer("yoda", served="claude-fable-5")],
        "council-finance": [http(500), answer("mundi", served="grok-4.6")],
        "council-spacial": [http(500), answer("windu", served="gemini-3.7-flash-high")],
        "council-mlx": [http(500)],
    })
    res = run_cli(["-s", "Yoda,Mundi,Windu", "--repoll", "2", "--require-all", "topic"],
                  proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert seat(payload, "Mundi")["response"] == "mundi" and seat(payload, "Mundi")["round"] == 1
    assert seat(payload, "Windu")["response"] == "windu"
    assert proxy.models_called().count("council-max-thinking") == 1   # answered seats are left alone
    assert payload["run"]["repoll_rounds_used"] == 1


def test_repoll_gives_a_truncated_seat_room_to_finish(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-code": [answer("cut", finish="length"),
                                         answer("complete", served="Muse-Glimmer-30B-4bit")]})
    res = run_cli(["-s", "Qui-Gon", "-t", "600", "--repoll", "1", "topic"],
                  proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert seat(json.loads(res.stdout), "Qui-Gon")["response"] == "complete"
    assert [c["max_tokens"] for c in proxy.calls] == [600, 1200]


# ───────────────────────── lane-aware dispatch ─────────────────────────
def test_single_lane_backend_never_has_two_client_requests_in_flight(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Put Qui-Gon on the cathedral lane next to Cilghal (both :1337/:6669 = one FIFO).
    monkeypatch.setitem(bs.SEATS["Qui-Gon"], "model", "council-mlx")
    script = {m: [answer(m)] for m in ("council-heretic", "council-mlx", "council-max-thinking",
                                       "council-brain", "council-finance")}
    proxy = FakeProxyd(script, hold_s=0.15)
    res = run_cli(["-s", "Cilghal,Qui-Gon,Yoda,Jocasta,Mundi", "topic"], proxy, monkeypatch,
                  full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert proxy.max_inflight["cathedral"] == 1          # serial within the single lane
    assert proxy.max_total_inflight >= 3                 # parallel across lanes


def test_concurrency_one_is_fully_serial(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = {m: [answer(m)] for m in ("council-max-thinking", "council-brain", "council-finance")}
    proxy = FakeProxyd(script, hold_s=0.1)
    res = run_cli(["-s", "Yoda,Jocasta,Mundi", "--concurrency", "1", "topic"], proxy, monkeypatch,
                  full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert proxy.max_total_inflight == 1


# ───────────────────────── Mini load gate ─────────────────────────
def test_parse_loadavg() -> None:
    assert bs._parse_loadavg("{ 6.44 7.77 11.97 }\n") == 6.44
    assert bs._parse_loadavg("") is None


def test_wait_for_load_blocks_until_the_mini_is_calm() -> None:
    clock = Clock()
    loads = iter([35.0, None, 25.0, 12.0])
    with patch.object(bs, "time", clock), patch.object(bs, "_sleep", clock.sleep):
        got = bs._wait_for_load("manoir", 20.0, 3600.0, probe=lambda _h: next(loads))
    assert got == 12.0
    assert clock.t == 1000.0 + 3 * bs.LOAD_POLL_S


def test_wait_for_load_gives_up_after_wait_max() -> None:
    clock = Clock()
    with patch.object(bs, "time", clock), patch.object(bs, "_sleep", clock.sleep), \
            pytest.raises(ProviderError, match="load gate never opened"):
        bs._wait_for_load("manoir", 20.0, 300.0, probe=lambda _h: 50.0)


def test_wait_load_needs_a_host(full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    monkeypatch.delenv("SANCTUM_COUNCIL_LOAD_HOST", raising=False)
    res = runner.invoke(app, ["brainstorm", "--wait-load", "20", "topic"])
    assert res.exit_code == 1


# ───────────────────────── JSON contract ─────────────────────────
def test_json_contract_keeps_old_keys_and_adds_the_new_ones(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-max-thinking": [answer("x", served="claude-fable-5")]})
    res = run_cli(["-s", "Yoda", "topic"], proxy, monkeypatch, full_instance_yaml)
    payload = json.loads(res.stdout)
    row = payload["seats"][0]
    for k in ("seat", "model_attempted", "model_used", "family", "status", "degraded",
              "fallback_from", "response", "error"):          # what the DF2 senders parse
        assert k in row, k
    assert row["status"] in ("ok", "degraded", "absent")      # still the three historical values
    assert row["model_used"] == "council-max-thinking"         # unchanged for an own-voice answer
    for k in ("outcome", "served_model", "finish_reason", "partial_response", "provenance",
              "lane", "budget_s", "elapsed_s", "attempts", "round"):
        assert k in row, k
    for k in ("truncated_seats", "diverted_seats", "own_voice_seats", "all_answered"):
        assert k in payload["diversity"], k


# ───────────────── review pass 2 (2026-09-19): one test per finding ─────────────────
def _legacy_answered(payload: dict[str, Any]) -> list[str]:
    """The exact predicate the DF2 senders use (send_when_ready.sh, repoll_q14a.sh)."""
    return [s["seat"] for s in payload["seats"] if (s.get("response") or "").strip()]


_HERETIC_DOWN = {"council-heretic": {"healthy": False, "error_rate_pct": 100}}


# F1 — proxyd's council-heretic entry has no read_timeout_secs, so a NON-streamed heretic
# request dies at proxyd's shared 120 s reqwest ceiling and is answered by
# council-local-think (diverted). A streamed one carries the cathedral's role chunk and
# 15 s keep-alives from before queue admission, so that inactivity clock never fires.
def test_heretic_seat_streams_by_default(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    # 20 keep-alives x 15 s = 300 s in the cathedral queue, far past proxyd's 120 s
    proxy = FakeProxyd({"council-heretic": [sse(["ablated view"], served="Qwen3.8-27B-4bit-champion-ablated",
                                                keepalives=20)]}, clock=clock)
    res = run_cli(["-s", "Cilghal", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 0, res.stdout + res.stderr
    cil = seat(json.loads(res.stdout), "Cilghal")
    assert cil["outcome"] == "answered" and cil["response"] == "ablated view"
    assert proxy.calls[0]["stream"] is True
    assert json.loads(res.stdout)["run"]["streamed"] == {"Cilghal": True}


def test_stream_policy_default_explicit_on_and_off() -> None:
    assert bs._seat_streams("Cilghal", None) is True
    assert bs._seat_streams("Mundi", None) is False
    assert bs._seat_streams("Yoda", None) is False
    assert bs._seat_streams("Cilghal", False) is False     # --no-stream wins
    assert bs._seat_streams("Mundi", True) is True         # --stream wins


def test_no_stream_turns_the_heretic_default_off(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-heretic": [answer("json", served="Qwen3.8-27B-4bit-champion-ablated")],
                        "council-finance": [answer("grok", served="grok-4.6")]})
    res = run_cli(["-s", "Cilghal,Mundi", "--no-stream", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert [c["stream"] for c in proxy.calls] == [False, False]


# F2 — --require-all must reject a substituted answer (diverted AND client fallback).
def test_require_all_rejects_a_diverted_seat(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-max-thinking": [answer("ok", served="claude-fable-5")],
                        "council-heretic": [answer("plain qwen", served="Qwen3.8-27B-4bit")]},
                       seats_health=_HERETIC_DOWN)
    res = run_cli(["-s", "Yoda,Cilghal", "--require-all", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2
    assert "Cilghal=diverted" in res.stderr
    div = json.loads(res.stdout)["diversity"]
    assert div["own_voice_seats"] == ["Yoda"] and div["all_answered"] is False


def test_require_all_rejects_a_client_fallback_answer(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-max-thinking": [answer("ok", served="claude-fable-5")],
                        "council-finance": [http(500)],
                        "council-mlx": [answer("qwen stand-in", served="Qwen3.8-27B-4bit")]})
    res = run_cli(["-s", "Yoda,Mundi", "--require-all", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2
    assert "Mundi=fallback" in res.stderr
    div = json.loads(res.stdout)["diversity"]
    assert div["own_voice_seats"] == ["Yoda"] and div["all_answered"] is False


# F3 — a substituted answer must not sit in `response`, where the DF2 senders count it.
def test_substituted_answers_stay_out_of_response(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-heretic": [answer("plain qwen", served="Qwen3.8-27B-4bit")],
                        "council-finance": [http(500)],
                        "council-mlx": [answer("qwen stand-in", served="Qwen3.8-27B-4bit")]},
                       seats_health=_HERETIC_DOWN)
    res = run_cli(["-s", "Cilghal,Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    payload = json.loads(res.stdout)
    cil, mundi = seat(payload, "Cilghal"), seat(payload, "Mundi")
    assert cil["outcome"] == "diverted" and cil["response"] is None
    assert cil["degraded_response"] == "plain qwen"
    assert mundi["outcome"] == "fallback" and mundi["response"] is None
    assert mundi["degraded_response"] == "qwen stand-in"
    assert _legacy_answered(payload) == []
    assert payload["diversity"]["answered_seats"] == 0
    assert res.exit_code == 2          # no seat answered in its own voice
    assert "own voice" in res.stderr


def test_own_voice_answer_still_fills_response(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-finance": [answer("grok says", served="grok-4.6")]})
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    row = seat(json.loads(res.stdout), "Mundi")
    assert res.exit_code == 0 and row["response"] == "grok says" and row["degraded_response"] is None


# F4 — proxyd moves requests between :3301 and :1337 (council-code -> council-mlx,
# council-heretic -> council-local-think -> council-code), so the two on-box lanes are
# ONE serialisation domain.
def test_code_and_cathedral_seats_never_overlap(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({m: [answer(m)] for m in ("council-heretic", "council-code")}, hold_s=0.2)
    res = run_cli(["-s", "Cilghal,Qui-Gon", "--no-stream", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert proxy.max_total_inflight == 1


# F11(C) — the client's own council-mlx fallback must take the on-box lock.
def test_client_fallback_waits_for_the_on_box_lock(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_heretic(p: FakeProxyd, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        time.sleep(0.4)               # Cilghal holds the cathedral while Mundi fails fast
        return answer("heretic")(p, request, body)

    proxy = FakeProxyd({"council-heretic": [slow_heretic],
                        "council-finance": [http(500)],
                        "council-mlx": [answer("qwen")]})
    res = run_cli(["-s", "Cilghal,Mundi", "--no-stream", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert "council-mlx" in proxy.models_called(), res.stdout + res.stderr
    assert proxy.max_inflight["cathedral"] == 1


# F5 — --repoll must not re-send a seat that cannot succeed, and must not re-send a
# timed-out seat into the same budget it already lost.
def test_repoll_skips_a_seat_proxyd_reports_unhealthy(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-heretic": [answer("plain qwen", served="Qwen3.8-27B-4bit")]},
                       seats_health=_HERETIC_DOWN)
    res = run_cli(["-s", "Cilghal", "--repoll", "2", "--repoll-wait", "0", "--require-all", "topic"],
                  proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2
    assert proxy.models_called() == ["council-heretic"]
    assert "unhealthy" in seat(json.loads(res.stdout), "Cilghal")["error"]


def test_repoll_resends_a_diverted_seat_once_proxyd_is_healthy_again(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-code": [answer("hosted", served="qwen/qwen3.6-plus"),
                                         answer("own", served="Muse-Glimmer-30B-4bit")]})
    res = run_cli(["-s", "Qui-Gon", "--repoll", "1", "--repoll-wait", "0", "--require-all", "topic"],
                  proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert seat(json.loads(res.stdout), "Qui-Gon")["response"] == "own"


def test_repoll_doubles_a_timed_out_seats_budget(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    proxy = FakeProxyd({"council-finance": [answer(latency=10_000)]}, clock=clock)
    res = run_cli(["-s", "Mundi", "--repoll", "2", "--repoll-wait", "0", "topic"],
                  proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 2
    reads = [c["timeout"]["read"] for c in proxy.calls]
    assert len(reads) == 3 and reads[1] >= 2 * reads[0] - 1 and reads[2] >= 2 * reads[1] - 1


# F6 — proxyd's 502 on chat/completions means its whole ladder failed (and it already
# paged Force Flow): never re-walk it, and skip a client fallback the ladder already tried.
_LADDER_502 = ("ALL SEATS FAILED requested=council-finance · [grok-46-metered✗HTTP 500 · "
               "glm-53✗HTTP 500 · council-mlx✗timeout] · last=timeout · double-esc or shrink context")


def test_proxyd_all_seats_failed_502_is_not_retried_nor_refallen(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-finance": [http(502, _LADDER_502)], "council-mlx": [answer("q")]})
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert proxy.models_called() == ["council-finance"]
    assert "ladder" in seat(json.loads(res.stdout), "Mundi")["error"]


def test_502_without_the_fallback_in_its_ladder_still_falls_back_once(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-finance": [http(502, "ALL SEATS FAILED requested=council-finance · "
                                                      "[grok-46-metered✗HTTP 500]")],
                        "council-mlx": [answer("q", served="Qwen3.8-27B-4bit")]})
    run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert proxy.models_called() == ["council-finance", "council-mlx"]


# F7 + F10 — by default no seat quits before proxyd does: council-finance 300 s,
# council-code / council-local-think 600 s, and the bridge seats' ttfb 600 s (the bridge's
# 300 s kill only starts once a subprocess slot frees).
def test_default_budgets_outlast_proxyds_own_limits(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-finance": [answer("x", served="grok-4.6")]})
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert json.loads(res.stdout)["run"]["budgets_s"]["Mundi"] > 300
    default_ceiling = inspect.signature(brainstorm_top).parameters["timeout"].default
    proxyd_limit = {"Yoda": 600, "Jocasta": 600, "Mothma": 600, "Mundi": 300, "Qui-Gon": 600,
                    "Cilghal": 600, "Windu": 120}
    for s, limit in proxyd_limit.items():
        assert bs._seat_budget(s, float(default_ceiling)) > limit, s


# F8 — a drop while /health answers at once is proxyd closing the response (e.g. an
# upstream error inside a raw stream passthrough), not a tunnel restart: never re-sent.
def test_drop_while_health_answers_is_a_server_abort_not_resent(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    proxy = FakeProxyd({"council-heretic": [disconnect()], "council-mlx": [disconnect()]}, clock=clock)
    res = run_cli(["-s", "Cilghal", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert proxy.models_called().count("council-heretic") == 1
    assert len(proxy.calls) <= 2
    cil = seat(json.loads(res.stdout), "Cilghal")
    assert "server-side" in cil["error"]


def test_drop_retry_gets_a_fresh_budget(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 200 s into a 310 s budget the tunnel restarts; the retry needs 150 s.
    clock = Clock()
    proxy = FakeProxyd({"council-finance": [disconnect(tunnel_down=1, after=200),
                                            answer("after", served="grok-4.6", latency=150)]}, clock=clock)
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml, clock)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert seat(json.loads(res.stdout), "Mundi")["response"] == "after"


# F9 — an answer that does not say which model produced it, on a seat proxyd reports
# unhealthy, is not proof of the seat's own voice.
def test_unreported_model_on_an_unhealthy_seat_is_not_own_voice(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-heretic": [answer("no model field")]}, seats_health=_HERETIC_DOWN)
    res = run_cli(["-s", "Cilghal", "--require-all", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2
    payload = json.loads(res.stdout)
    cil = seat(payload, "Cilghal")
    assert cil["outcome"] == "diverted" and cil["response"] is None
    # never credited to the heretic family it could not prove
    assert cil["family"] != "heretic" and "heretic" not in payload["diversity"]["degraded_families"]


def test_unreported_model_on_a_healthy_seat_is_still_an_answer() -> None:
    v, _ = bs._provenance("council-heretic", "heretic", "cathedral", None,
                          {"council-heretic": {"healthy": True, "error_rate_pct": 0}})
    assert v == "unreported"


# F11(B) — a later, worse re-poll result never replaces a better earlier one.
def test_repoll_keeps_the_better_earlier_result(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    proxy = FakeProxyd({"council-code": [answer("hosted", served="qwen/qwen3.6-plus"), answer(latency=10_000)]},
                       clock=clock)
    res = run_cli(["-s", "Qui-Gon", "--repoll", "1", "--repoll-wait", "0", "topic"],
                  proxy, monkeypatch, full_instance_yaml, clock)
    qg = seat(json.loads(res.stdout), "Qui-Gon")
    assert len(proxy.calls) == 2
    assert qg["outcome"] == "diverted" and qg["degraded_response"] == "hosted"


def test_prefer_ranks_outcomes() -> None:
    def mk(o: str, status: Any, content: str | None) -> Any:
        return bs.SeatResult("Cilghal", "council-heretic", None, content, None, "qwen", status, True,
                             "heretic", outcome=o)
    diverted = mk("diverted", bs.Status.DEGRADED, "text")
    timeout = mk("timeout", bs.Status.ABSENT, None)
    answered = mk("answered", bs.Status.OK, "own")
    assert bs._prefer(diverted, timeout) is diverted
    assert bs._prefer(diverted, answered) is answered


# ───────── review pass 3 (2026-09-20): WHO ANSWERED is proxyd's word, not a guess ─────────
# On 09-20 council-heretic returned 503 and proxyd answered from council-local-think:
# HTTP 200, and the SAME model name ("Qwen3.8-27B-4bit") in the body. The client could
# only say "ambiguous" — and counted it. A newer proxyd (a) says on the wire which seat
# answered (x-sanctum-seated / x-sanctum-route-chain) and (b) honours an opt-in
# `x-sanctum-no-fallback: 1`: that seat or a 503, never a substitute. The client must work
# against BOTH the old proxyd (no such headers) and the new one.
_SHARED = "Qwen3.8-27B-4bit"   # what :6669 (heretic) and :1337 (council-local-think) both report
_HEALTHY = {"council-heretic": {"healthy": True, "error_rate_pct": 0}}


def _seated(seat_key: str, requested: str, *failed: str) -> dict[str, str]:
    """The response headers the new proxyd sets on /v1/chat/completions."""
    h = {"x-sanctum-requested": requested, "x-sanctum-seated": seat_key,
         "x-sanctum-route-chain": " > ".join([*failed, seat_key])}
    if seat_key != requested:
        h["x-sanctum-route"] = "diverted"
    return h


def _strict_503(requested: str) -> Any:
    return http(503, f"ROUTE FAILED requested={requested} · [{requested}✗HTTP 503] · last=HTTP 503",
                headers=_seated("none", requested, requested))


# — the request header —
def test_every_seat_request_refuses_a_substitute_by_default(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # JSON seat, streamed seat, and the client's OWN council-mlx fallback request alike.
    proxy = FakeProxyd({"council-max-thinking": [answer("y", served="claude-fable-5")],
                        "council-heretic": [sse(["c"], served="Qwen3.8-27B-4bit-champion-ablated")],
                        "council-finance": [http(500)],
                        "council-mlx": [answer("q", served=_SHARED)]})
    res = run_cli(["-s", "Yoda,Cilghal,Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert sorted(set(proxy.models_called())) == ["council-finance", "council-heretic",
                                                  "council-max-thinking", "council-mlx"]
    assert [c["no_fallback"] for c in proxy.calls] == ["1"] * len(proxy.calls), proxy.calls
    assert json.loads(res.stdout)["run"]["allow_fallback"] is False


def test_allow_fallback_omits_the_strict_header(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-max-thinking": [answer("y", served="claude-fable-5")],
                        "council-heretic": [sse(["c"], served="Qwen3.8-27B-4bit-champion-ablated")]})
    res = run_cli(["-s", "Yoda,Cilghal", "--allow-fallback", "topic"], proxy, monkeypatch,
                  full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert len(proxy.calls) == 2
    assert [c["no_fallback"] for c in proxy.calls] == [None, None]
    assert json.loads(res.stdout)["run"]["allow_fallback"] is True


def test_ask_sends_the_strict_header_by_default_and_not_when_fallback_is_allowed() -> None:
    for opts, want in ((None, {"x-sanctum-no-fallback": "1"}),
                       (bs.AskOptions(allow_fallback=True), {})):
        client = _DropThenAnswer()
        client.posts = 1                     # skip the scripted drop: answer at once
        kw = {"opts": opts} if opts else {}
        r = bs._ask(client, "Mundi", "council-finance", "lens", "topic", 600,  # type: ignore[arg-type]
                    time.monotonic() + 100, **kw)
        assert r.content == "after the drop"
        assert client.sent_headers == [want]


# — seated == requested: proven genuine —
@pytest.mark.parametrize("health", [_HEALTHY, _HERETIC_DOWN, None])
def test_attested_seat_is_a_match_even_with_the_shared_weights_model_name(health: Any) -> None:
    # Without the header this exact body reads "ambiguous" (healthy) or "diverted" (unhealthy).
    verdict, note = bs._provenance("council-heretic", "heretic", "cathedral", _SHARED, health,
                                   seated="council-heretic", route_chain="council-heretic")
    assert verdict == "match"
    assert "attested" in (note or "") and "council-heretic" in (note or "")


@pytest.mark.parametrize("flags", [["--no-stream"], []], ids=["json", "sse"])
def test_attested_heretic_answer_is_counted_and_recorded_as_proven(
    flags: list[str], full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hdrs = _seated("council-heretic", "council-heretic")
    step = (answer("ablated view", served=_SHARED, headers=hdrs) if flags
            else sse(["ablated ", "view"], served=_SHARED, headers=hdrs))
    proxy = FakeProxyd({"council-heretic": [step]}, seats_health=_HEALTHY)
    res = run_cli(["-s", "Cilghal", "--require-all", *flags, "topic"], proxy, monkeypatch,
                  full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    cil = seat(payload, "Cilghal")
    assert cil["outcome"] == "answered" and cil["response"] == "ablated view"
    assert cil["provenance"] == "match" and cil["provenance_basis"] == "attested"
    assert cil["seated"] == "council-heretic" and cil["route_chain"] == "council-heretic"
    assert "attested" in cil["note"]
    assert payload["diversity"]["own_voice_seats"] == ["Cilghal"]


# — seated != requested: proven NOT genuine —
@pytest.mark.parametrize("flags", [["--no-stream"], []], ids=["json", "sse"])
def test_attested_diversion_is_not_counted_even_when_the_body_looks_like_the_seat(
    flags: list[str], full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The 09-20 incident, replayed against the new proxyd: heretic failed, council-local-think
    # answered, same model name, seat HEALTHY in /health -> the old client said "ambiguous"
    # and counted it as Cilghal's vote.
    hdrs = _seated("council-local-think", "council-heretic", "council-heretic")
    step = (answer("plain qwen", served=_SHARED, headers=hdrs) if flags
            else sse(["plain ", "qwen"], served=_SHARED, headers=hdrs))
    proxy = FakeProxyd({"council-heretic": [step]}, seats_health=_HEALTHY)
    res = run_cli(["-s", "Cilghal", *flags, "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2                      # no seat answered in its own voice
    payload = json.loads(res.stdout)
    cil = seat(payload, "Cilghal")
    assert cil["status"] == "degraded" and cil["outcome"] == "diverted"
    assert cil["response"] is None and cil["degraded_response"] == "plain qwen"
    assert cil["provenance"] == "diverted" and cil["provenance_basis"] == "attested"
    assert cil["seated"] == "council-local-think"
    assert cil["model_used"] == "council-local-think" and cil["served_model"] == _SHARED
    assert cil["route_chain"] == "council-heretic > council-local-think"
    assert "council-local-think" in cil["note"] and "council-heretic > council-local-think" in cil["note"]
    assert cil["family"] == "qwen" and cil["fallback_from"] == "heretic"
    assert _legacy_answered(payload) == []
    assert payload["diversity"]["own_voice_seats"] == []
    assert payload["diversity"]["diverted_seats"] == ["Cilghal"]
    assert payload["diversity"]["answered_seats"] == 0


@pytest.mark.parametrize(("seat_model", "designed", "lane", "served", "seated", "chain", "names"), [
    # a claude-looking body from ANOTHER claude seat is still not this seat's answer
    ("council-max-thinking", "claude", "bridge", "claude-fable-5", "council-brain",
     "council-max-thinking > council-brain", "council-brain"),
    # no body model at all: the header alone settles it
    ("council-finance", "grok", "grok", None, "council-mlx", "council-finance > council-mlx",
     "council-mlx"),
    # a 200 that claims nobody answered is not an answer either
    ("council-finance", "grok", "grok", "grok-4.6", "none", "council-finance > none", "no seat"),
])
def test_attested_diversion_verdicts(seat_model: str, designed: str, lane: str, served: str | None,
                                     seated: str, chain: str, names: str) -> None:
    verdict, note = bs._provenance(seat_model, designed, lane, served, {},
                                   seated=seated, route_chain=chain)
    assert verdict == "diverted"
    assert names in (note or "") and chain in (note or "")


def test_attested_match_still_records_a_body_that_contradicts_it() -> None:
    # The header settles WHICH SEAT answered; what that seat's proxyd entry points at is
    # a different question, and a contradicting body is written down, not dropped.
    verdict, note = bs._provenance("council-finance", "grok", "grok", "Qwen3.8-27B-4bit", {},
                                   seated="council-finance", route_chain="council-finance")
    assert verdict == "match" and "attested" in (note or "")
    assert "Qwen3.8-27B-4bit (qwen), not a grok model" in (note or "")
    verdict, note = bs._provenance("council-code", "codestral", "code", "qwen/qwen3.6-plus", {},
                                   seated="council-code", route_chain="council-code")
    assert verdict == "match" and "HOSTED" in (note or "")
    # the seat's own model, or the weights it shares with its fallback: nothing to flag
    for served in ("Qwen3.8-27B-4bit-champion-ablated", _SHARED, None):
        _, note = bs._provenance("council-heretic", "heretic", "cathedral", served, {},
                                 seated="council-heretic", route_chain="council-heretic")
        assert "but its backend" not in (note or ""), served


def test_route_headers_tolerate_old_proxyd_and_header_less_doubles() -> None:
    assert bs._route_headers(None) == (None, None)
    assert bs._route_headers(object()) == (None, None)
    assert bs._route_headers({}) == (None, None)
    assert bs._route_headers(httpx.Headers({"X-Sanctum-Seated": " council-code ",
                                            "x-sanctum-route-chain": ""})) == ("council-code", None)


def test_attested_hosted_stand_in_still_says_the_prompt_left_the_box() -> None:
    verdict, note = bs._provenance("council-code", "codestral", "code", "qwen/qwen3.6-plus", {},
                                   seated="qwen36-plus", route_chain="council-code > qwen36-plus")
    assert verdict == "diverted" and "HOSTED" in (note or "") and "left the box" in (note or "")


# — seated ABSENT (the old proxyd): today's heuristics, byte for byte —
_UNHEALTHY_H = {"council-heretic": {"healthy": False, "error_rate_pct": 100}}
_HEALTHY_H = {"council-heretic": {"healthy": True, "error_rate_pct": 0}}
# (args) -> (verdict, note), captured from the pre-header `_provenance` (main @ c0a8994).
_LEGACY_VERDICTS: list[tuple[tuple[Any, ...], tuple[str, str | None]]] = [
    (("council-heretic", "heretic", "cathedral", None, _UNHEALTHY_H),
     ("diverted", "the backend did not report which model answered, while proxyd /health reports "
                  "council-heretic unhealthy (error_rate 100%) — most likely a proxyd fallback")),
    (("council-heretic", "heretic", "cathedral", None, _HEALTHY_H), ("unreported", None)),
    (("council-heretic", "heretic", "cathedral", None, None), ("unreported", None)),
    (("council-code", "codestral", "code", "qwen/qwen3.6-plus", {}),
     ("diverted", "proxyd served qwen/qwen3.6-plus, a HOSTED model, for a local seat — "
                  "the prompt left the box")),
    (("council-max-thinking", "claude", "bridge", "claude-fable-5", {}), ("match", None)),
    (("council-code", "codestral", "code", "Muse-Glimmer-30B-4bit", {}), ("match", None)),
    (("council-finance", "grok", "grok", "grok-4.6", {}), ("match", None)),
    (("council-spacial", "gemini", "agy", "gemini-3.7-flash-high", {}), ("match", None)),
    (("council-heretic", "heretic", "cathedral", "Qwen3.8-27B-4bit-champion-ablated", _UNHEALTHY_H),
     ("match", None)),
    (("council-heretic", "heretic", "cathedral", "Qwen3.8-27B-4bit", _HEALTHY_H),
     ("ambiguous", "served Qwen3.8-27B-4bit: weights council-heretic shares with its proxyd "
                   "fallback — cannot prove which one answered")),
    (("council-heretic", "heretic", "cathedral", "Qwen3.8-27B-4bit", None),
     ("ambiguous", "served Qwen3.8-27B-4bit: weights council-heretic shares with its proxyd "
                   "fallback — cannot prove which one answered")),
    (("council-heretic", "heretic", "cathedral", "Qwen3.8-27B-4bit", _UNHEALTHY_H),
     ("diverted", "served Qwen3.8-27B-4bit: weights council-heretic shares with its proxyd "
                  "fallback, while proxyd /health reports council-heretic unhealthy "
                  "(error_rate 100%) — a fallback answer")),
    (("council-finance", "grok", "grok", "Qwen3.8-27B-4bit", {}),
     ("diverted", "proxyd served Qwen3.8-27B-4bit (qwen), not a grok model")),
    (("council-finance", "grok", "grok", "z-ai/glm-5.3", {}),
     ("diverted", "proxyd served z-ai/glm-5.3 (hosted), not a grok model")),
    (("council-finance", "grok", "grok", "mystery-model", {}), ("unrecognized", None)),
    (("council-mlx", "qwen", "cathedral", "Qwen3.8-27B-4bit", {}), ("match", None)),
]


@pytest.mark.parametrize(("args", "expected"), _LEGACY_VERDICTS)
def test_legacy_call_shape_gives_every_legacy_verdict_unchanged(
    args: tuple[Any, ...], expected: tuple[str, str | None]
) -> None:
    # Green BEFORE and AFTER the header work: the five-argument call is untouched.
    assert bs._provenance(*args) == expected


@pytest.mark.parametrize(("args", "expected"), _LEGACY_VERDICTS)
@pytest.mark.parametrize("blank", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_without_the_seated_header_every_legacy_verdict_is_unchanged(
    args: tuple[Any, ...], expected: tuple[str, str | None], blank: str | None
) -> None:
    assert bs._provenance(*args, seated=blank, route_chain=blank) == expected


def test_old_proxyd_answer_is_recorded_as_inferred(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No x-sanctum-* headers at all: exactly the old verdict, and the record SAYS it is a guess.
    proxy = FakeProxyd({"council-heretic": [answer("who knows", served=_SHARED)]},
                       seats_health=_HEALTHY)
    res = run_cli(["-s", "Cilghal", "--no-stream", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    cil = seat(json.loads(res.stdout), "Cilghal")
    assert cil["outcome"] == "answered" and cil["provenance"] == "ambiguous"
    assert cil["provenance_basis"] == "inferred"
    assert cil["seated"] is None and cil["route_chain"] is None


def test_json_rows_always_carry_the_attestation_fields(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-max-thinking": [answer("x", served="claude-fable-5")],
                        "council-finance": [http(500)], "council-mlx": [http(500)]})
    res = run_cli(["-s", "Yoda,Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    for row in json.loads(res.stdout)["seats"]:
        for k in ("seated", "route_chain", "provenance_basis"):
            assert k in row, (row["seat"], k)
    mundi = seat(json.loads(res.stdout), "Mundi")
    assert mundi["provenance_basis"] is None       # no answer, no attestation: nothing established


# — a strict 503: the seat is ABSENT, said so by name, and never re-fired —
def test_strict_503_is_an_absent_seat_named_as_such_and_not_retried(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-finance": [_strict_503("council-finance")],
                        "council-mlx": [_strict_503("council-mlx")]})
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2
    # ONE request per seat: the legacy 503 back-off retry must not fire on a strict 503
    assert proxy.models_called() == ["council-finance", "council-mlx"]
    mundi = seat(json.loads(res.stdout), "Mundi")
    assert mundi["status"] == "absent" and mundi["outcome"] == "http_error"
    assert mundi["response"] is None and mundi["degraded_response"] is None
    assert "strict-seat failure" in mundi["error"] and "council-finance" in mundi["error"]
    assert "rate-limited" not in mundi["error"]
    # proxyd's own word that NOBODY answered is kept; there is no answer to judge
    assert mundi["seated"] == "none" and mundi["route_chain"] == "council-finance > none"
    assert mundi["provenance"] is None and mundi["provenance_basis"] is None
    assert "strict" in res.stderr and "--allow-fallback would only buy a stand-in" in res.stderr


def test_strict_503_on_the_streamed_heretic_falls_to_a_flagged_stand_in_once(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 09-20 under the new regime: the heretic 503s -> nothing answers in its name. The
    # client's own council-mlx stand-in is asked once, flagged, and never counted.
    proxy = FakeProxyd({"council-heretic": [_strict_503("council-heretic")],
                        "council-mlx": [answer("qwen stand-in", served=_SHARED,
                                               headers=_seated("council-mlx", "council-mlx"))]},
                       seats_health=_HEALTHY)
    res = run_cli(["-s", "Cilghal", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2
    assert proxy.models_called() == ["council-heretic", "council-mlx"]
    payload = json.loads(res.stdout)
    cil = seat(payload, "Cilghal")
    assert cil["outcome"] == "fallback" and cil["response"] is None
    assert cil["degraded_response"] == "qwen stand-in"
    assert cil["seated"] == "council-mlx" and cil["provenance_basis"] == "attested"
    assert "strict-seat failure" in cil["note"]       # WHY the home seat is missing survives
    assert payload["diversity"]["own_voice_seats"] == []


def test_a_503_without_the_seated_header_keeps_its_one_legacy_retry(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The old proxyd (and the new one's policy / USD-cap 503s) send no x-sanctum-seated.
    proxy = FakeProxyd({"council-finance": [http(503, "busy"), answer("numbers", served="grok-4.6")]})
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert proxy.models_called() == ["council-finance", "council-finance"]
    proxy = FakeProxyd({"council-finance": [http(503, "busy")], "council-mlx": [http(503, "busy")]})
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    err = seat(json.loads(res.stdout), "Mundi")["error"]
    assert "rate-limited (503)" in err and "strict-seat" not in err


def test_seated_none_503_is_only_a_strict_failure_when_strictness_was_asked_for(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = FakeProxyd({"council-finance": [_strict_503("council-finance"),
                                            answer("numbers", served="grok-4.6")]})
    res = run_cli(["-s", "Mundi", "--allow-fallback", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 0, res.stdout + res.stderr
    assert proxy.models_called() == ["council-finance", "council-finance"]   # legacy transient retry


@pytest.mark.parametrize("hdrs", [None, _seated("none", "council-finance", "council-finance")],
                         ids=["old-proxyd", "new-proxyd"])
def test_413_is_never_retried(
    hdrs: dict[str, str] | None, full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A too-large prompt is too large every time (Qui-Gon 413 storm, 2026-08-18).
    proxy = FakeProxyd({"council-finance": [http(413, "context too large", headers=hdrs)],
                        "council-mlx": [http(413, "context too large")]})
    res = run_cli(["-s", "Mundi", "topic"], proxy, monkeypatch, full_instance_yaml)
    assert res.exit_code == 2
    assert proxy.models_called() == ["council-finance", "council-mlx"]
    assert "HTTP 413" in seat(json.loads(res.stdout), "Mundi")["error"]
