"""``sanctum council`` — the Jedi Council chamber in a terminal.

Interactive REPL with seat switching (``/yoda``, ``/windu``, …) and a
fan-out mode (``/council <question>`` or one-shot ``sanctum council "q"``)
that puts the question to EVERY seat in parallel and closes with a Yoda
synthesis. Seats are proxyd :4040 council models (Anthropic dialect); each
Jedi is a persona system prompt on top of a seat. Yoda and Mundi share a
brain but never a voice — the neurodiversity doctrine in one line.

Transport: plain httpx against proxyd's ``/v1/messages`` (SSE streaming in
the REPL, buffered in fan-out). The key rides ``x-api-key`` — resolved from
``$SANCTUM_PROXY_KEY``, then the keychain, then a CLI identifier (proxyd's
inference path is currently lenient; the resolution order means this keeps
working the day it gets strict).
"""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from sanctum_cli.commands import banner, council_tools

console = Console()

PROXYD_URL_ENV = "SANCTUM_PROXYD_URL"
PROXYD_KEY_ENV = "SANCTUM_PROXY_KEY"
_DEFAULT_PROXYD = "http://127.0.0.1:4040"
_KEYCHAIN_SERVICE = "sanctum-proxy-client"
_ANTHROPIC_VERSION = "2023-06-01"
_MAX_TOKENS = 1200
DEFAULT_SEAT = "yoda"

TOOL_CALL_CAP = 8
# Absolute backstop on tool-loop POSTs. Every legitimate path returns within
# TOOL_CALL_CAP + 1 iterations; this hard bound exists purely so a malformed,
# looping, or adversarial model can never drive the buffered loop unbounded and
# balloon memory. Earned the hard way 2026-06-12: a missing-`name` tool_use
# block looped the turn — growing `convo` each pass — until the Mini OOM'd and
# panicked. Defense-in-depth, independent of the per-call cap.
MAX_TOOL_LOOP_ITERATIONS = TOOL_CALL_CAP * 2


class ToolsRejected(RuntimeError):  # noqa: N818
    """Raised when the bridge rejects a tools-bearing request (4xx)."""


@dataclass(frozen=True)
class Seat:
    """One council chair: a persona riding a proxyd model."""

    label: str
    model: str
    persona: str
    style: str  # rich color for the nameplate
    verb: str  # what the seat does while it thinks ("ponders", …)
    tools: tuple[str, ...] = ()


SEATS: dict[str, Seat] = {
    "yoda": Seat(
        label="Yoda",
        model="council-max-thinking",
        persona=(
            "You are Yoda, Grand Master of the Sanctum Jedi Council — the wise"
            " synthesist. Sanctum is a self-hosted family AI and haus-ops"
            " platform on a Mac Mini ('manoir') guarding a family network."
            " Speak with inverted Yoda syntax sparingly (one or two phrases,"
            " never a parody), favour wisdom, trade-offs, and the long view."
            " Be concise: a council chamber, not a lecture hall. On a"
            " system-health question (e.g. 'is the Signal link working?'),"
            " do NOT improvise a diagnosis from memory or old relics: use"
            " your instruments to observe live state, or name the authoritative"
            " check the operator should run and say plainly what you observed."
        ),
        style="green",
        verb="ponders",
        tools=("sanctum_status", "sanctum_doctor", "agent_list", "logs_tail"),
    ),
    "windu": Seat(
        label="Windu",
        model="council-spacial",
        persona=(
            "You are Mace Windu of the Sanctum Jedi Council — security and"
            " network defence. Threat-model first: fail-closed boundaries,"
            " bypass vectors, blast radius. You respect convenience only after"
            " it has been searched for weapons. Direct, short sentences."
        ),
        style="magenta",
        verb="deliberates",
    ),
    "quigon": Seat(
        label="Qui-Gon",
        model="council-code",
        persona=(
            "You are Qui-Gon Jinn of the Sanctum Jedi Council — infrastructure"
            " and code. Pragmatic builder: smallest correct change, root cause"
            " over symptom, measure before optimizing. Offer the concrete next"
            " step, not the grand refactor."
        ),
        style="cyan",
        verb="builds",
    ),
    "mundi": Seat(
        label="Ki-Adi-Mundi",
        model="council-max-thinking",
        persona=(
            "You are Ki-Adi-Mundi of the Sanctum Jedi Council — finance and"
            " resource intelligence (two brains: analytical and intuitive)."
            " Costs, budgets, capacity, return on effort. Numbers when you"
            " have them, ranges when you don't, never vibes dressed as data."
        ),
        style="yellow",
        verb="computes",
    ),
    "cilghal": Seat(
        label="Cilghal",
        model="council-mlx",
        persona=(
            "You are Cilghal of the Sanctum Jedi Council — health and"
            " diagnostics of the haus systems. Symptoms, evidence, honest"
            " uncertainty. You never declare a system healthy that you have"
            " not observed."
        ),
        style="blue",
        verb="examines",
    ),
    "jocasta": Seat(
        label="Jocasta",
        model="council-brain",
        persona=(
            "You are Jocasta Nu of the Sanctum Jedi Council — the keeper of"
            " records on the Mac. You hold the haus's memory: iMessage,"
            " Calendar, Contacts, Mail, CRM, and the tech-lookout. You answer"
            " from what is written and recorded, cite the source when you can,"
            " and say plainly when a record is missing rather than guess."
            " Precise, archival, a touch wry."
        ),
        style="bright_white",
        verb="consults the archives",
    ),
    "mothma": Seat(
        label="Mon Mothma",
        model="council-brain",
        persona=(
            "You are Mon Mothma of the Sanctum Council — chief of operations."
            " Not a Jedi but the steady hand that keeps the haus running:"
            " deployments and cutovers, runbooks, drift and stability windows,"
            " backups and restore, secret rotation, upgrades, and the"
            " operational state of every service. You ask the operator's"
            " questions — is it deployed, is it stable, is it backed up, what"
            " is the runbook, and what breaks under load or at 3 a.m. Calm,"
            " organized, procedural."
        ),
        style="red",
        verb="checks the runbook",
        tools=("sanctum_status", "sanctum_doctor", "agent_list", "logs_tail"),
    ),
}


def thinking_markup(seat: Seat) -> str:
    """The status line shown while a seat thinks — in character, in colour."""
    return f"[{seat.style}]{seat.label} {seat.verb}…[/]"


# ── Pure REPL parsing ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ReplAction:
    kind: str  # say | switch | switch_say | council | seats | new | quit | noop | error
    arg: str = ""


def parse_repl_input(line: str) -> ReplAction:
    """Map one REPL line to an action. Unknown slash-commands are errors,
    never silently sent to a model (a typo'd /windou must not leak to chat)."""
    text = line.strip()
    if not text:
        return ReplAction("noop")
    if text.startswith("@council"):
        return ReplAction("council", text[len("@council") :].strip())
    if not text.startswith("/"):
        return ReplAction("say", text)
    head, _, rest = text[1:].partition(" ")
    head = head.lower()
    rest = rest.strip()
    if head in ("quit", "exit", "q"):
        return ReplAction("quit")
    if head == "new":
        return ReplAction("new")
    if head == "seats":
        return ReplAction("seats")
    if head == "council":
        return ReplAction("council", rest)
    if head in SEATS:
        return ReplAction("switch_say", rest) if rest else ReplAction("switch", head)
    return ReplAction("error", head)


class Transcript:
    """Shared conversation history, capped to the last N turns."""

    def __init__(self, max_turns: int = 20) -> None:
        self._max_turns = max_turns
        self._msgs: list[dict[str, str]] = []

    def add(self, role: str, content: str) -> None:
        self._msgs.append({"role": role, "content": content})
        keep = self._max_turns * 2
        if len(self._msgs) > keep:
            self._msgs = self._msgs[-keep:]

    def messages(self) -> list[dict[str, str]]:
        return list(self._msgs)

    def clear(self) -> None:
        self._msgs = []


# ── Transport (Anthropic dialect against proxyd) ─────────────────────


def _proxyd_url() -> str:
    return os.environ.get(PROXYD_URL_ENV, _DEFAULT_PROXYD).rstrip("/")


def _proxy_key() -> str:
    key = os.environ.get(PROXYD_KEY_ENV, "").strip()
    if key:
        return key
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return "sanctum-cli-council"


def _headers() -> dict[str, str]:
    return {
        "x-api-key": _proxy_key(),
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def sse_text_delta(line: str) -> str | None:
    """Extract the text from one Anthropic SSE line; None for anything else."""
    if not line.startswith("data: "):
        return None
    try:
        event = json.loads(line[6:])
    except ValueError:
        return None
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    text = delta.get("text")
    return text if isinstance(text, str) and text else None


def _stream(seat: Seat, messages: list[dict[str, str]], *, system: str) -> Iterator[str]:
    """Yield text deltas from a streaming /v1/messages call."""
    payload = {
        "model": seat.model,
        "max_tokens": _MAX_TOKENS,
        "system": system,
        "messages": messages,
        "stream": True,
    }
    with (
        httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client,
        client.stream(
            "POST", f"{_proxyd_url()}/v1/messages", headers=_headers(), json=payload
        ) as resp,
    ):
        if resp.status_code != 200:
            resp.read()
            raise RuntimeError(f"{seat.label} seat HTTP {resp.status_code}: {resp.text[:160]}")
        for line in resp.iter_lines():
            delta = sse_text_delta(line)
            if delta:
                yield delta


def _complete(seat: Seat, messages: list[dict[str, str]], *, system: str) -> str:
    """Buffered (non-streaming) completion — used by the fan-out."""
    payload = {
        "model": seat.model,
        "max_tokens": _MAX_TOKENS,
        "system": system,
        "messages": messages,
    }
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        resp = client.post(f"{_proxyd_url()}/v1/messages", headers=_headers(), json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"{seat.label} seat HTTP {resp.status_code}: {resp.text[:160]}")
    data = resp.json()
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def _post_with_tools(
    seat: Seat,
    messages: list[dict[str, object]],
    *,
    system: str,
    tools: list[dict[str, object]],
) -> dict[str, object]:
    """Buffered POST with a tools array; returns the raw response dict.

    On 400-499 raises ToolsRejected so the caller can degrade to the
    streaming path (the primary bridge can't tool yet — phase-0 finding).
    """
    payload: dict[str, object] = {
        "model": seat.model,
        "max_tokens": _MAX_TOKENS,
        "system": system,
        "messages": messages,
        "tools": tools,
    }
    with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        resp = client.post(f"{_proxyd_url()}/v1/messages", headers=_headers(), json=payload)
    status = resp.status_code
    if 400 <= status <= 499:
        raise ToolsRejected(f"HTTP {status}: {resp.text[:160]}")
    if status != 200:
        raise RuntimeError(f"{seat.label} seat HTTP {status}: {resp.text[:160]}")
    result: dict[str, object] = resp.json()
    return result


def _persona(seat: Seat, *, armed: bool | None = None) -> str:
    """Compose the system prompt for a seat.

    Armed seats get an instruments clause enumerating their tools plus the
    live-state contract. Unarmed seats get an honest no-tools declaration.

    ``armed=None`` (default) derives the arming from whether the seat has
    tools configured. Pass ``armed=False`` to force the unarmed clause even
    for an armed seat (e.g. the REPL fallback after ToolsRejected).
    """
    is_armed = seat.tools if armed is None else armed
    if is_armed:
        mounted = council_tools.mount_tools(seat.tools, is_tty=True)
        tool_lines = "\n".join(f"- {t.name} — {t.description}" for t in mounted)
        return (
            seat.persona
            + "\n\nInstruments you have:\n"
            + tool_lines
            + "\n\nClaims about live state must come from a tool result in this"
            " conversation, not from memory. A capability you lack, name the"
            " operator's command instead. Tool inputs are plain JSON — no"
            " styling, no inversion."
        )
    return (
        seat.persona + "\n\nYou are a chat seat with NO tools — never claim to have run a"
        " command, read a file, or observed live state."
    )


def _tool_turn(seat: Seat, messages: list[dict[str, object]]) -> str:
    """Buffered Anthropic tool-use loop for armed seats. The cap and the
    breaker keep a confused model from sawing at the instruments all
    night (council #6). Statuses run sequentially — verb dots while the
    model thinks, instrument dots while a tool runs — never nested.

    Per-block extraction is hardened: malformed tool_use blocks (stringified
    JSON input, missing id, missing name) degrade per-block with an audited
    error result fed back to the model rather than aborting the turn.
    """
    mounted = council_tools.mount_tools(seat.tools, is_tty=console.is_terminal)
    specs: list[dict[str, object]] = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in mounted
    ]
    convo: list[dict[str, object]] = list(messages)
    session = f"repl-{os.getpid()}"
    calls = 0
    consecutive_errors = 0
    synth_counter = 0
    first_post = True
    iterations = 0
    while True:
        # Absolute backstop: no model — malformed, looping, or adversarial —
        # may drive this loop past a bounded number of POSTs. Every legitimate
        # path returns well within MAX_TOOL_LOOP_ITERATIONS; this guard is pure
        # defense-in-depth against a future logic slip re-opening the balloon.
        iterations += 1
        if iterations > MAX_TOOL_LOOP_ITERATIONS:
            return "(tool loop bound reached — answering without further tools)"
        with console.status(
            thinking_markup(seat), spinner="simpleDotsScrolling", spinner_style=seat.style
        ):
            try:
                data = _post_with_tools(seat, convo, system=_persona(seat), tools=specs)
            except ToolsRejected:
                if first_post:
                    raise
                raise RuntimeError("mid-turn tools failure: seat rejected tools mid-loop") from None
        first_post = False
        blocks = data.get("content", [])
        if not isinstance(blocks, list):
            blocks = []
        tool_uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        if data.get("stop_reason") != "tool_use" or not tool_uses:
            answer = "".join(
                b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            return answer if answer else "(no answer)"

        results: list[dict[str, object]] = []
        echo_tool_blocks: list[dict[str, object]] = []
        budget_hit = False
        for block in tool_uses:
            # The breaker is checked for EVERY block — malformed ones included —
            # so an all-error stream (a failing tool OR a malformed block) can
            # never loop unbounded. This is the gate the 2026-06-12 balloon
            # slipped past: malformed blocks used to skip it entirely.
            if consecutive_errors >= 2:
                budget_hit = True
                break

            # ── Extract fields with per-block malformation handling ──────
            block_id: str | None = None
            block_name: str | None = None
            block_input: dict[str, object] | None = None
            parse_error: str | None = None

            if not isinstance(block, dict):
                parse_error = f"tool_use block is not a dict: {type(block).__name__}"
            else:
                raw_id = block.get("id")
                block_id = str(raw_id) if raw_id is not None else None

                raw_name = block.get("name")
                block_name = str(raw_name) if raw_name is not None else None
                if block_name is None:
                    parse_error = "tool_use block missing 'name'"
                elif block_id is None:
                    parse_error = "tool_use block missing 'id'"

                raw_input = block.get("input")
                if isinstance(raw_input, str):
                    # Stringified JSON — attempt rescue (classic local-model malformation)
                    try:
                        parsed = json.loads(raw_input)
                        if isinstance(parsed, dict):
                            block_input = parsed
                        else:
                            parse_error = (
                                f"tool_use 'input' parsed to non-dict: {type(parsed).__name__}"
                            )
                    except json.JSONDecodeError as exc:
                        parse_error = f"tool_use 'input' is unparseable string: {exc}"
                elif isinstance(raw_input, dict):
                    block_input = raw_input
                elif raw_input is None:
                    block_input = {}
                else:
                    parse_error = (
                        f"tool_use 'input' has unexpected type: {type(raw_input).__name__}"
                    )

            if parse_error is not None:
                audit_tool = block_name if block_name else "<malformed>"
                council_tools.audit(
                    council_tools.AUDIT_LEDGER,
                    seat=seat.label,
                    session=session,
                    tool=audit_tool,
                    params={},
                    kind="unknown",
                    mode="auto",
                    outcome="error",
                    duration_ms=0,
                )
                consecutive_errors += 1
                # A block with no 'name' is unrecoverable: it can't be run, and
                # echoing a nameless tool_use is a protocol 400. Drop it — no
                # echo, no tool_result. If the whole batch drops away, the
                # empty-results guard below ends the turn (no loop-back).
                if block_name is None:
                    continue
                # Recoverable malformation (bad input, or missing id with a name):
                # synthesize an id when absent so the echoed assistant block and
                # its paired error tool_result reference the same id, keeping the
                # next POST a valid Anthropic exchange.
                if block_id is None:
                    synth_counter += 1
                    block_id = f"toolu_synth_{synth_counter}"
                echo_tool_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block_id,
                        "name": block_name,
                        "input": block_input or {},
                    }
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block_id,
                        "content": f"malformed tool call: {parse_error}",
                        "is_error": True,
                    }
                )
                continue

            # ── Well-formed block — apply the per-call cap ───────────────
            calls += 1
            if calls > TOOL_CALL_CAP:
                budget_hit = True
                break
            with console.status(
                f"[{seat.style}]{seat.label} consults the instruments… ({block_name})[/]",
                spinner="simpleDotsScrolling",
                spinner_style=seat.style,
            ):
                result = council_tools.run_tool(
                    str(block_name),
                    dict(block_input or {}),
                    seat=seat.label,
                    session=session,
                )
            consecutive_errors = consecutive_errors + 1 if result.is_error else 0
            # Echo the NORMALIZED block (rescued input as a dict), paired 1:1
            # with its tool_result by id.
            echo_tool_blocks.append(
                {
                    "type": "tool_use",
                    "id": block_id,
                    "name": block_name,
                    "input": block_input or {},
                }
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
            )

        if budget_hit:
            # Distinct strings so the caller can tell the two failure modes apart.
            if consecutive_errors >= 2:
                return "(instrument errors — answering without further tools)"
            return "(tool call cap reached — partial answer only)"

        # Nothing to feed back — every block was a nameless drop. Do NOT loop:
        # a user message with an empty content array is a protocol 400, and
        # re-POSTing the identical context invites the model to repeat the same
        # malformed batch forever (the balloon). End the turn honestly instead.
        if not results:
            return "(no usable tool calls — answering without tools)"

        # Echo the normalized assistant turn — original text blocks plus the
        # normalized tool_use blocks — then the paired tool_results.
        text_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        convo.append({"role": "assistant", "content": text_blocks + echo_tool_blocks})
        convo.append({"role": "user", "content": results})


# ── Fan-out: the full council deliberates ─────────────────────────────


@dataclass
class CouncilResult:
    answers: dict[str, str] = field(default_factory=dict)
    synthesis: str = ""


def council_ask(question: str) -> CouncilResult:
    """Put one question to every seat in parallel; Yoda synthesizes.

    A dead seat answers with a ⚠ marker instead of sinking the session —
    the council proceeds with the survivors (quorum over perfection).
    """
    result = CouncilResult()
    ask = [{"role": "user", "content": question}]

    def one(jedi: str) -> tuple[str, str]:
        seat = SEATS[jedi]
        try:
            return seat.label, _complete(seat, ask, system=seat.persona)
        except Exception as e:
            return seat.label, f"⚠ seat unavailable: {e}"

    with ThreadPoolExecutor(max_workers=len(SEATS)) as pool:
        for label, answer in pool.map(one, SEATS):
            result.answers[label] = answer

    voices = "\n\n".join(
        f"## {label}\n{answer}"
        for label, answer in result.answers.items()
        if not answer.startswith("⚠")
    )
    yoda = SEATS["yoda"]
    synth_prompt = (
        f"The council was asked: {question}\n\nThe seats answered:\n\n{voices}\n\n"
        "Synthesize the council's view in at most five sentences: where they"
        " agree, the sharpest disagreement, and your ruling."
    )
    try:
        result.synthesis = _complete(
            yoda, [{"role": "user", "content": synth_prompt}], system=yoda.persona
        )
    except Exception as e:
        result.synthesis = f"⚠ synthesis unavailable: {e}"
    return result


# ── Rendering + REPL ──────────────────────────────────────────────────


def _render_council(question: str, result: CouncilResult) -> None:
    console.print()
    console.print(f"[bold dim]The council considers:[/] {question}")
    for seat in SEATS.values():
        answer = result.answers.get(seat.label, "")
        console.print(
            Panel(
                Text(answer),
                title=f"[{seat.style}]{seat.label}[/]",
                title_align="left",
                border_style=seat.style,
            )
        )
    if result.synthesis:
        console.print(
            Panel(
                Text(result.synthesis),
                title="[bold green]⚖ Council synthesis (Yoda)[/]",
                title_align="left",
                border_style="green",
            )
        )


def _print_seats(active: str) -> None:
    for jedi, seat in SEATS.items():
        marker = "●" if jedi == active else "○"
        console.print(f"  [{seat.style}]{marker} /{jedi:<8}[/] {seat.label} — {seat.model}")


def _say_turn(
    seat: Seat,
    transcript: Transcript,
    raw_arg: str,
) -> None:
    """Execute one say/switch_say turn: tool loop (armed) or streaming.

    Extracted from ``_repl`` so that Ctrl-C behaviour and the streaming
    path can be unit-tested without wiring up a full interactive REPL loop.
    Returns normally after the turn completes (including on Ctrl-C abort).
    """
    transcript.add("user", raw_arg)
    if seat.tools:
        try:
            try:
                answer = _tool_turn(seat, cast("list[dict[str, object]]", transcript.messages()))
            except KeyboardInterrupt:
                console.print("\n[dim](turn aborted)[/]")
                transcript.add("assistant", "(turn aborted)")
                return
            answer = answer if answer else "(no answer)"
            # Markup-safe answer printing: nameplate via markup, answer as plain Text
            nameplate = Text.from_markup(f"[{seat.style}]{seat.label}:[/] ")
            console.print(nameplate.append_text(Text(answer)), soft_wrap=True)
            transcript.add("assistant", answer)
            return
        except ToolsRejected:
            console.print("[dim](seat's model declines tools — chat only this turn)[/]")
            # Fall through to the streaming path with unarmed persona
        except Exception as e:
            console.print(f"[red]⚠ {escape(str(e))}[/]")
            transcript.add("assistant", "(seat unavailable)")
            return

    # streaming path — runs when unarmed OR after ToolsRejected
    # Pass armed=False when degrading from ToolsRejected so the model gets
    # the honest unarmed persona rather than the instruments clause.
    persona_system = _persona(seat, armed=False) if seat.tools else _persona(seat)
    chunks: list[str] = []
    try:
        # _stream is lazy — the request fires on the first pull, so the
        # status animates exactly across the model's thinking dead-air
        # and vanishes the moment the first word lands.
        stream = _stream(seat, transcript.messages(), system=persona_system)
        try:
            with console.status(
                thinking_markup(seat), spinner="simpleDotsScrolling", spinner_style=seat.style
            ):
                first = next(stream, None)
        except KeyboardInterrupt:
            console.print("\n[dim](turn aborted)[/]")
            transcript.add("assistant", "(turn aborted)")
            return
        console.print(f"[{seat.style}]{seat.label}:[/] ", end="")
        if first is not None:
            chunks.append(first)
            # Deltas are model output — print them as Text so a stray ``[/bad]``
            # in the stream can't raise MarkupError and crash the REPL turn.
            console.print(Text(first), end="", soft_wrap=True)
            try:
                for delta in stream:
                    chunks.append(delta)
                    console.print(Text(delta), end="", soft_wrap=True)
            except KeyboardInterrupt:
                console.print("\n[dim](turn aborted)[/]")
                answer = "".join(chunks) if chunks else "(turn aborted)"
                transcript.add("assistant", answer)
                return
        console.print()
    except Exception as e:
        # Escape the error text: a network/HTTP message can itself carry markup
        # (``[/bad]``), and an unescaped interpolation would raise a SECOND
        # MarkupError out of the handler and take down the whole REPL.
        console.print(f"\n[red]⚠ {escape(str(e))}[/]")
        transcript.add("assistant", "(seat unavailable)")
        return
    answer = "".join(chunks)
    answer = answer if answer else "(no answer)"
    transcript.add("assistant", answer)


def _repl() -> None:
    active = DEFAULT_SEAT
    transcript = Transcript()
    banner.render_banner(console)
    console.print(
        "[dim]The chamber is in session. "
        "/yoda /windu /quigon /mundi /cilghal /jocasta /mothma switch seats · "
        "/council <q> asks everyone · /new clears · /quit leaves[/]"
    )
    while True:
        seat = SEATS[active]
        try:
            line = console.input(f"[{seat.style}]({active})[/] ❯ ")  # noqa: RUF001
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]The chamber empties.[/]")
            return
        action = parse_repl_input(line)
        if action.kind == "noop":
            continue
        if action.kind == "quit":
            console.print("[dim]May the Force be with you.[/]")
            return
        if action.kind == "new":
            transcript.clear()
            console.print("[dim]Transcript cleared.[/]")
            continue
        if action.kind == "seats":
            _print_seats(active)
            continue
        if action.kind == "error":
            console.print(f"[red]No seat or command named /{action.arg}[/] — try /seats")
            continue
        if action.kind == "switch":
            active = line.strip()[1:].split(" ")[0].lower()
            console.print(f"[dim]{SEATS[active].label} has the floor.[/]")
            continue
        if action.kind == "council":
            if not action.arg:
                console.print("[red]Usage: /council <question>[/]")
                continue
            with console.status("[dim]The council deliberates…[/]"):
                result = council_ask(action.arg)
            _render_council(action.arg, result)
            continue
        if action.kind == "switch_say":
            active = line.strip()[1:].split(" ")[0].lower()
        # say / switch_say → delegate to _say_turn
        seat = SEATS[active]
        _say_turn(seat, transcript, action.arg)


def council_command(
    question: Annotated[
        str | None,
        typer.Argument(help="One-shot: put this question to the full council and exit."),
    ] = None,
) -> None:
    """``sanctum council`` — interactive chamber; with a question, full fan-out."""
    if question:
        with console.status("[dim]The council deliberates…[/]"):
            result = council_ask(question)
        _render_council(question, result)
        return
    _repl()
