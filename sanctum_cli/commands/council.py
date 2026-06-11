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
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from collections.abc import Iterator

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from sanctum_cli.commands import banner

console = Console()

PROXYD_URL_ENV = "SANCTUM_PROXYD_URL"
PROXYD_KEY_ENV = "SANCTUM_PROXY_KEY"
_DEFAULT_PROXYD = "http://127.0.0.1:4040"
_KEYCHAIN_SERVICE = "sanctum-proxy-client"
_ANTHROPIC_VERSION = "2023-06-01"
_MAX_TOKENS = 1200
DEFAULT_SEAT = "yoda"


@dataclass(frozen=True)
class Seat:
    """One council chair: a persona riding a proxyd model."""

    label: str
    model: str
    persona: str
    style: str  # rich color for the nameplate


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
            " Be concise: a council chamber, not a lecture hall. You are a chat"
            " seat with NO tools — never claim to have run a command, read a"
            " file, or observed live state. On a system-health question (e.g."
            " 'is the Signal link working?'), do NOT improvise a diagnosis from"
            " memory or old relics: name the authoritative check the operator"
            " should run (for Signal that is `yoda-chat status` on the VM) and"
            " say plainly that you cannot observe it from this chamber. Crying"
            " wolf from guesswork is worse than admitting you must check."
        ),
        style="green",
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
        style="bright_blue",
    ),
}


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
            capture_output=True, text=True, timeout=5, check=False,
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
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client, client.stream(
        "POST", f"{_proxyd_url()}/v1/messages", headers=_headers(), json=payload
    ) as resp:
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
        f"## {label}\n{answer}" for label, answer in result.answers.items()
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
        console.print(Panel(Text(answer), title=f"[{seat.style}]{seat.label}[/]",
                            title_align="left", border_style=seat.style))
    if result.synthesis:
        console.print(Panel(Text(result.synthesis), title="[bold green]⚖ Council synthesis (Yoda)[/]",
                            title_align="left", border_style="green"))


def _print_seats(active: str) -> None:
    for jedi, seat in SEATS.items():
        marker = "●" if jedi == active else "○"
        console.print(f"  [{seat.style}]{marker} /{jedi:<8}[/] {seat.label} — {seat.model}")


def _repl() -> None:
    active = DEFAULT_SEAT
    transcript = Transcript()
    banner.render_banner(console)
    console.print("[dim]The chamber is in session. "
                  "/yoda /windu /quigon /mundi /cilghal /jocasta /mothma switch seats · "
                  "/council <q> asks everyone · /new clears · /quit leaves[/]")
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
        # say / switch_say → stream from the active seat with shared history
        seat = SEATS[active]
        transcript.add("user", action.arg)
        console.print(f"[{seat.style}]{seat.label}:[/] ", end="")
        chunks: list[str] = []
        try:
            for delta in _stream(seat, transcript.messages(), system=seat.persona):
                chunks.append(delta)
                console.print(delta, end="", soft_wrap=True)
            console.print()
        except Exception as e:
            console.print(f"\n[red]⚠ {e}[/]")
            transcript.add("assistant", "(seat unavailable)")
            continue
        transcript.add("assistant", "".join(chunks))


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
