"""``sanctum brainstorm "<topic>"`` — convene the heterogeneous Jedi Council.

Fans a topic out to each council seat IN PARALLEL and prints every Jedi's take,
so the operator can pressure-test a plan against distinct model families at
once. Per the neurodiversity doctrine, a single model in seven robes is not a
council — so this command treats **model family** as a first-class value and
makes any collapse toward homogeneity *impossible to miss*:

  - Each seat is tagged with its resolved family; diversity is counted in
    FAMILIES, never seats (7 seats all answering as Qwen = 1 family = homogenized).
  - A fallback / degradation / absence can never masquerade as a healthy voice:
    degraded seats render with a loud badge, absent seats in red with the verbatim
    error, and a council-summary footer states families AND seat-liveness.
  - Losing any DESIGNED family (e.g. Gemini dies) always surfaces a notice, even
    if the family floor is still met.

Roster (aligned with OpenClaw agents + proxyd):
  Yoda=max-thinking (Fable), Windu=spacial (Gemini), Qui-Gon=code (Devstral),
  Mundi=finance (Grok), Cilghal=heretic (27B :6669), Jocasta+Mothma=brain (Opus 5).

Seats route through the house smart-router *proxyd* (``:4040``), which owns auth
and per-seat backend routing. A seat whose model fails or returns empty degrades
to the always-on local Qwen fallback — but only as a flagged last resort, and a
duplicate family is never counted as new diversity.

proxyd serves a single TLS front door (PQC) on :4040 with a cert issued by the
Sanctum mTLS Root CA (``CN=sanctum-mlx``, not the access host), so we verify the
chain against ``~/.sanctum/certs/ca.crt`` with hostname binding off — strictly
safer than disabling verification.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import enum
import os
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel

from sanctum_cli import config, telemetry
from sanctum_cli.errors import LocalError, NetworkError, ProviderError, SanctumError, UserError
from sanctum_cli.haus import haus_required

if TYPE_CHECKING:
    from sanctum_cli.config import Telemetry as TelemetryConfig

console = Console()
err_console = Console(stderr=True)

DEFAULT_URL = os.environ.get("SANCTUM_COUNCIL_URL", "https://127.0.0.1:4040")
DEFAULT_CACERT = Path(
    os.environ.get("SANCTUM_COUNCIL_CACERT", str(Path.home() / ".sanctum/certs/ca.crt"))
)
FALLBACK_MODEL = os.environ.get("COUNCIL_FALLBACK_MODEL", "council-mlx")


# ─── Model-family resolution (neurodiversity is computed on FAMILY, not strings) ───
_FAMILY_BY_MODEL: dict[str, str] = {
    "council-max-thinking": "claude",
    "council-brain": "claude",
    "council-ops": "claude",
    "council-code": "codestral",
    "council-devstral": "codestral",
    "council-finance": "grok",
    "council-spacial": "gemini",
    "council-secure": "gemini",
    "council-heretic": "heretic",
    "cilghal-health": "heretic",
    "council-mlx": "qwen",
    "council-local-think": "qwen",
    "gemini-31-pro": "gemini",
    "gemini-3-pro": "gemini",
    "gemini-25-pro": "gemini",
    "grok-best": "grok",
    "grok-oauth": "grok",
    "grok-4.5": "grok",
}
# Env override so a new backend can be tagged without a release: "model=family,..."
for _pair in os.environ.get("COUNCIL_FAMILY_MAP", "").split(","):
    if "=" in _pair:
        _k, _v = _pair.split("=", 1)
        _FAMILY_BY_MODEL[_k.strip()] = _v.strip()

# Ordered substring patterns (first hit wins; specific families before generic tokens).
_FAMILY_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("council-max-thinking", "council-brain", "council-ops", "max-thinking", "opus", "sonnet", "haiku", "claude"), "claude"),
    (("council-heretic", "cilghal-health", "heretic"), "heretic"),
    (("gemini", "council-spacial", "council-secure"), "gemini"),
    (("codestral", "council-code", "council-devstral", "devstral", "mistral"), "codestral"),
    (("council-finance", "grok-best", "grok-oauth", "grok"), "grok"),
    (("council-mlx", "council-local-think", "mlx", "qwen"), "qwen"),
)


def _family_of(model: str | None) -> str:
    """Resolve a model string to its family. None -> 'absent'; unrecognized ->
    a DISTINCT 'unknown:<model>' bucket (never silently folded into a real family)."""
    if model is None:
        return "absent"
    if model in _FAMILY_BY_MODEL:
        return _FAMILY_BY_MODEL[model]
    low = model.lower()
    for tokens, fam in _FAMILY_PATTERNS:
        if any(t in low for t in tokens):
            return fam
    return f"unknown:{model}"


def _real_family(family: str) -> bool:
    """A family that counts toward heterogeneity. Excludes the 'absent' sentinel
    and per-model 'unknown:*' buckets — two unrecognized models are NOT two voices."""
    return family != "absent" and not family.startswith("unknown:")


FALLBACK_FAMILY = _family_of(FALLBACK_MODEL)

# Quorum floor (in FAMILIES). 0/CLI --min-families => auto min(MIN_FAMILIES, designed).
MIN_FAMILIES = int(os.environ.get("SANCTUM_COUNCIL_MIN_FAMILIES", "3"))


# ─── Seats — neurodiversity doctrine (aligned with OpenClaw agents) ───
# Yoda=Fable-max, Windu=Gemini, Qui-Gon=Devstral, Mundi=Grok, Cilghal=Heretic,
# Jocasta+Mothma=Opus-medium. Claude appears on multiple seats (different effort
# tiers) — family accounting dedups; the footer flags designed redundancy.
SEATS: dict[str, dict[str, str]] = {
    "Yoda": {
        "model": os.environ.get("YODA_MODEL", "council-max-thinking"),
        "lens": (
            "You are Yoda, chief of the Council. Step all the way back and judge the WHOLE "
            "approach: is this the right path, what is being missed, what is the wisest move? "
            "Strategy over detail."
        ),
    },
    "Windu": {
        "model": os.environ.get("WINDU_MODEL", "council-spacial"),
        "lens": (
            "You are Windu, security and correctness, with fresh eyes. Name the non-obvious "
            "failure mode, the wrong assumption, the threat. Be blunt and direct."
        ),
    },
    "Qui-Gon": {
        "model": os.environ.get("QUI_GON_MODEL", "council-code"),
        "lens": (
            "You are Qui-Gon, the Council's coder and infrastructure pragmatist. Focus on "
            "implementation: what is technically hard, fragile, or inefficient, and the concrete "
            "code-level lever being overlooked."
        ),
    },
    "Mundi": {
        "model": os.environ.get("MUNDI_MODEL", "council-finance"),
        "lens": (
            "You are Ki-Adi-Mundi, the data and analysis seat. Challenge the premise with "
            "evidence: what does the data actually say, what is being measured wrong, where is "
            "the proof? Numbers over vibes. Costs, capacity, ROI."
        ),
    },
    "Cilghal": {
        "model": os.environ.get("CILGHAL_MODEL", "council-heretic"),
        "lens": (
            "You are Cilghal, architect and healer. Focus on system invariants, correctness, and "
            "long-term health — of the haus and of Bert. What breaks the contracts or the design "
            "over time? Symptoms, evidence, honest uncertainty."
        ),
    },
    "Jocasta": {
        "model": os.environ.get("JOCASTA_MODEL", "council-brain"),
        "lens": (
            "You are Jocasta Nu, keeper of records. What is written and recorded — iMessage, "
            "Calendar, Contacts, Mail, CRM, tech-lookout. Cite sources when you can; say plainly "
            "when a record is missing rather than guess."
        ),
    },
    "Mothma": {
        "model": os.environ.get("MOTHMA_MODEL", "council-brain"),
        "lens": (
            "You are Mon Mothma, chief of operations. Deployments, cutovers, runbooks, drift, "
            "backups, secret rotation, upgrades. Is it deployed, is it stable, is it backed up, "
            "what breaks at 3 a.m.?"
        ),
    },
}

# Canonical DESIGNED family per seat — names a lost voice ("gemini voice lost") even
# when the seat's model is env-overridden to an unrecognized string.
_CANONICAL_SEAT_FAMILY: dict[str, str] = {
    "Yoda": "claude",
    "Windu": "gemini",
    "Qui-Gon": "codestral",
    "Mundi": "grok",
    "Cilghal": "heretic",
    "Jocasta": "claude",
    "Mothma": "claude",
}
# Derive each seat's (possibly env-overridden) family from its RESOLVED model.
for _name, _seat in SEATS.items():
    _seat["family"] = _family_of(_seat["model"])


# ─── Voice-preservation tuning (all env-overridable) ───
THINKING_SEATS = frozenset(
    s.strip() for s in os.environ.get("COUNCIL_THINKING_SEATS", "Windu").split(",") if s.strip()
)
THINKING_BUDGET_FLOOR = int(os.environ.get("COUNCIL_THINKING_FLOOR", "3072"))
THINKING_BUDGET_ESCALATED = int(os.environ.get("COUNCIL_THINKING_ESCALATED", "6144"))
_TRANSIENT_STATUS = frozenset({429, 503})
RETRY_BACKOFF_CAP_S = float(os.environ.get("COUNCIL_RETRY_BACKOFF_CAP_S", "5"))
# Per-family per-call timeouts: opus-via-Max-bridge legitimately thinks ~120s; local
# seats should fail fast. The operator --timeout is a CEILING for non-claude families.
SEAT_TIMEOUTS: dict[str, float] = {
    "claude": 150.0,
    "gemini": 60.0,
    "codestral": 200.0,  # Devstral cold-prefill can exceed 3 min after :3301 restart
    "qwen": 60.0,
    "grok": 150.0,
    "heretic": 90.0,
}
SEAT_TIMEOUT_FLOOR = 30.0

SHARED_INSTRUCTION = (
    "The operator is brainstorming and wants the Council to make sure nothing is missed. "
    "Answer CONCISELY from YOUR lens — no preamble. State: (A) the single biggest thing being "
    "missed or gotten wrong, (B) one concrete idea or angle not yet considered, and (C) the "
    "failure mode most likely to waste effort. A confident, specific disagreement is far more "
    "valuable than agreement."
)


class Status(enum.StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    ABSENT = "absent"


@dataclass(frozen=True)
class SeatResult:
    seat: str
    model_attempted: str
    model_used: str | None
    content: str | None
    error: str | None
    family: str            # family that ANSWERED (or 'absent')
    status: Status
    degraded: bool
    fallback_from: str | None  # the DESIGNED family that was lost, when degraded/absent


@dataclass(frozen=True)
class Diversity:
    designed: frozenset[str]
    achieved: frozenset[str]
    degraded_families: frozenset[str]
    absent_seats: tuple[str, ...]
    degraded_seats: tuple[str, ...]
    redundant: dict[str, list[str]]   # real family -> [seats] when >1 chosen seat shares it
    answered_seats: int
    total_seats: int


# ─── low-level helpers (unit-testable, patchable) ───
def _ssl_verify(cacert: Path) -> ssl.SSLContext | bool:
    """Verify proxyd's chain against the Sanctum CA, skipping hostname binding
    (cert CN=sanctum-mlx != access host). Falls back to no verification only when
    the CA file is absent."""
    if cacert.exists():
        ctx = ssl.create_default_context(cafile=str(cacert))
        ctx.check_hostname = False
        return ctx
    return False


def _reasoning_tokens(data: dict[str, Any]) -> int:
    """Reasoning/thought token count, tolerating OpenAI-compat AND raw Gemini shapes."""
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    for val in (
        details.get("reasoning_tokens"),
        usage.get("reasoning_tokens"),
        usage.get("thoughtsTokenCount"),
        (data.get("usageMetadata") or {}).get("thoughtsTokenCount"),
    ):
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return 0


def _sleep(seconds: float) -> None:
    """Indirection so tests can patch out real sleeping."""
    time.sleep(seconds)


def _retry_after_seconds(exc: httpx.HTTPStatusError) -> float:
    """Honor Retry-After on a transient response, capped at RETRY_BACKOFF_CAP_S."""
    raw = exc.response.headers.get("Retry-After", "") if exc.response is not None else ""
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        secs = 1.0
    return min(max(secs, 0.0), RETRY_BACKOFF_CAP_S)


def _seat_budget(seat: str, ceiling: float) -> float:
    """Per-seat wall-clock budget (whole attempt chain). The operator --timeout is a
    ceiling for non-claude families; claude is never clipped below its think budget."""
    fam = SEATS[seat]["family"]
    base = SEAT_TIMEOUTS.get(fam, SEAT_TIMEOUT_FLOOR)
    if fam != "claude":
        base = min(base, ceiling)
    return max(SEAT_TIMEOUT_FLOOR, base)


def _body(model: str, lens: str, topic: str, max_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": f"{lens}\n\n{SHARED_INSTRUCTION}"},
            {"role": "user", "content": topic},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }


def _ask(
    client: httpx.Client,
    seat: str,
    model: str,
    lens: str,
    topic: str,
    max_tokens: int,
    deadline: float,
) -> SeatResult:
    """Resolve one seat. ALWAYS returns a SeatResult — never raises (a raised _ask
    would nuke the whole fan-out). Chain: home model (thinking-floored, one same-model
    escalation on starvation, one bounded retry on transient) -> Qwen fallback (flagged)
    -> ABSENT. Every non-home answer is flagged DEGRADED; no answer is ABSENT with the
    verbatim error preserved."""
    home_family = _CANONICAL_SEAT_FAMILY.get(seat, SEATS.get(seat, {}).get("family", _family_of(model)))
    last_error = "no attempt made"

    def _remaining() -> float:
        return deadline - time.monotonic()

    def _try(candidate: str, tokens: int) -> tuple[str | None, str | None, bool]:
        """One candidate, with at most one transient/empty-codestral retry.
        Returns (content|None, error|None, starved). Never raises."""
        nonlocal last_error
        per_call = max(1.0, min(_remaining(), SEAT_TIMEOUTS.get(_family_of(candidate), SEAT_TIMEOUT_FLOOR)))
        for attempt in range(2):
            if _remaining() <= 0:
                last_error = "per-seat deadline exceeded"
                return None, last_error, False
            try:
                resp = client.post("/v1/chat/completions", json=_body(candidate, lens, topic, tokens),
                                   timeout=per_call)
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                first = choices[0] if choices else {}
                content = (first.get("message") or {}).get("content", "") or ""
                if content.strip():
                    return content.strip(), None, False
                reasoning = _reasoning_tokens(data)
                if reasoning > 0:   # thinking starvation — the load-bearing signal
                    last_error = f"empty (thinking starvation, reasoning={reasoning})"
                    return None, last_error, True
                last_error = f"empty response from {candidate} (finish={first.get('finish_reason')})"
                # codestral empties ~1/3 of the time — one quick own-model retry
                if _family_of(candidate) == "codestral" and attempt == 0 and _remaining() > 1:
                    _sleep(min(0.5, RETRY_BACKOFF_CAP_S))
                    continue
                return None, last_error, False
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                if code in _TRANSIENT_STATUS:
                    last_error = f"rate-limited ({code})"
                    if attempt == 0 and _remaining() > 1:
                        _sleep(_retry_after_seconds(exc))
                        continue
                else:
                    last_error = f"HTTP {code}"
                return None, last_error, False
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                return None, last_error, False
        return None, last_error, False

    try:
        is_thinking = seat in THINKING_SEATS
        first_budget = max(max_tokens, THINKING_BUDGET_FLOOR) if is_thinking else max_tokens

        # 1. home model
        content, err, starved = _try(model, first_budget)
        if content:
            return SeatResult(seat, model, model, content, None, _family_of(model), Status.OK, False, None)

        # 2. same-model escalation on genuine thinking starvation (before any fallback)
        if starved and _remaining() > 5:
            content, err2, _ = _try(model, max(THINKING_BUDGET_ESCALATED, first_budget))
            if content:
                return SeatResult(seat, model, model, content, None, _family_of(model), Status.OK, False, None)
            err = err2 or err

        # 3. Qwen fallback — flagged DEGRADED, only if home is not already the fallback
        if model != FALLBACK_MODEL and _remaining() > 5:
            content, ferr, _ = _try(FALLBACK_MODEL, max_tokens)
            if content:
                return SeatResult(seat, model, FALLBACK_MODEL, content, None,
                                  FALLBACK_FAMILY, Status.DEGRADED, True, home_family)
            err = err or ferr

        # 4. no voice at all
        return SeatResult(seat, model, None, None, err or last_error, "absent", Status.ABSENT, True, home_family)
    except Exception as exc:
        return SeatResult(seat, model, None, None, f"{type(exc).__name__}: {exc}",
                          "absent", Status.ABSENT, True, home_family)


def _resolve_topic(topic: str | None, file: Path | None) -> str:
    if topic and file:
        raise UserError("pass either a positional topic or --file, not both")
    if file is not None:
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise UserError(f"cannot read --file {file}: {exc}", fix="check the path and permissions") from exc
    elif topic is not None:
        text = topic
    else:
        if sys.stdin.isatty():
            raise UserError("no topic provided",
                            fix='pass a topic: sanctum brainstorm "..."  (or pipe text on stdin)')
        text = sys.stdin.read()
    if not text.strip():
        raise UserError("empty topic", fix='pass a non-empty topic: sanctum brainstorm "..."')
    return text


def _select_seats(seats: str | None) -> list[str]:
    if not seats:
        return list(SEATS)
    by_lower = {name.lower(): name for name in SEATS}
    chosen: list[str] = []
    for raw in seats.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        if key not in by_lower:
            raise UserError(f"unknown seat: {raw.strip()!r} (expected any of: {', '.join(SEATS)})")
        if by_lower[key] not in chosen:
            chosen.append(by_lower[key])
    if not chosen:
        raise UserError("no valid seats selected", fix=f"choose from: {', '.join(SEATS)}")
    return chosen


def _assess_diversity(results: list[SeatResult], chosen: list[str]) -> Diversity:
    """Pure: compute DESIGNED vs ACHIEVED family heterogeneity. Counts DISTINCT real
    families as sets — a fallback to an already-present family adds nothing."""
    designed = frozenset(
        f for c in chosen if _real_family(f := SEATS.get(c, {}).get("family", "absent"))
    )
    achieved = frozenset(r.family for r in results if r.status is Status.OK and _real_family(r.family))
    degraded_families = frozenset(
        r.family for r in results if r.status is Status.DEGRADED and r.content and _real_family(r.family)
    )
    absent_seats = tuple(r.seat for r in results if r.status is Status.ABSENT)
    degraded_seats = tuple(r.seat for r in results if r.status is Status.DEGRADED)
    answered = sum(1 for r in results if r.content)

    fam_to_seats: dict[str, list[str]] = {}
    for c in chosen:
        f = SEATS.get(c, {}).get("family", "absent")
        if _real_family(f):
            fam_to_seats.setdefault(f, []).append(c)
    redundant = {f: s for f, s in fam_to_seats.items() if len(s) > 1}

    return Diversity(designed, achieved, degraded_families, absent_seats, degraded_seats,
                     redundant, answered, len(chosen))


# ─── rendering ───
def _seat_panel(r: SeatResult, redundant: dict[str, list[str]]) -> Panel:
    if r.status is Status.ABSENT:
        lost = r.fallback_from or "?"
        body = f"[red]ABSENT — {lost} voice lost.[/]\n{r.error or 'no response'}"
        return Panel(body, title=f"[bold red][ABSENT] {r.seat}  ({lost})[/]", title_align="left", border_style="red")
    if r.status is Status.DEGRADED:
        lost = r.fallback_from or "?"
        banner = f"[yellow]DEGRADED — {lost} voice unreachable; answered as {r.model_used} ({r.family}).[/]"
        if r.family == FALLBACK_FAMILY:
            banner += "\n[yellow]This DUPLICATES an existing family — no new diversity.[/]"
        body = f"{banner}\n\n{r.content}"
        return Panel(body, title=f"[bold yellow]{r.seat}  [DEGRADED {lost}->{r.family}][/]",
                     title_align="left", border_style="yellow")
    # OK — but flag a configured redundancy (e.g. Yoda+Mundi both claude)
    title = f"[bold]{r.seat}[/]  [dim]{r.model_used} · {r.family}[/]"
    body = r.content or ""
    peers = [s for s in redundant.get(r.family, []) if s != r.seat]
    if peers:
        body = f"[dim]redundant — duplicates {r.family} (also: {', '.join(peers)}); adds no diversity.[/]\n\n{body}"
    return Panel(body, title=title, title_align="left", border_style="cyan")


def _emit_diversity(div: Diversity, effective: frozenset[str], floor: int, ok: bool, lost: list[str]) -> None:
    """One-line council summary to stderr (so --json stdout stays pure JSON)."""
    head = (f"Neurodiversity: {len(effective)}/{floor} families "
            f"(designed {len(div.designed)}: {', '.join(sorted(div.designed)) or '—'}; "
            f"answered {', '.join(sorted(div.achieved)) or '—'}) | "
            f"{div.answered_seats}/{div.total_seats} seats answered")
    detail = []
    for s in div.degraded_seats:
        detail.append(f"{s} fell back")
    for s in div.absent_seats:
        detail.append(f"{s} ABSENT")
    tail = (" | " + "; ".join(detail)) if detail else ""
    if ok and not lost:
        err_console.print(f"[bold cyan]{head}{tail}[/]")
    else:
        err_console.print(f"[bold]{head}{tail}[/]")
        if lost:
            err_console.print(f"[bold red]NOTICE — designed families lost: {', '.join(lost)} "
                              f"(answered on no seat's own model).[/]")
        if not ok:
            err_console.print(f"[bold red]WARNING — council homogenized: {len(effective)} distinct "
                              f"effective families < floor {floor}. A duplicate-family fallback is NOT "
                              f"heterogeneity.[/]")


def _is_connect_error(error: str | None) -> bool:
    if not error:
        return False
    return any(tok in error for tok in (
        "ConnectError", "ConnectTimeout", "ConnectionRefused", "Connection refused",
        "[Errno 61]", "[Errno 111]", "getaddrinfo", "Name or service",
        "SSLError", "CERTIFICATE_VERIFY_FAILED",
    ))


def _load_telemetry() -> TelemetryConfig | None:
    """Best-effort: brainstorm is usable without an instance.yaml (e.g. from a laptop
    over Tailscale), so a missing/invalid config disables telemetry, not the command."""
    try:
        return config.load().cli.telemetry
    except SanctumError:
        return None


def _summon(
    url: str,
    verify: ssl.SSLContext | bool,
    chosen: list[str],
    text: str,
    max_tokens: int,
    timeout: float,
) -> list[SeatResult]:
    client_timeout = max([_seat_budget(s, timeout) for s in chosen] + [SEAT_TIMEOUT_FLOOR]) + RETRY_BACKOFF_CAP_S + 5
    try:
        with (
            httpx.Client(base_url=url, timeout=client_timeout, verify=verify) as client,
            concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(chosen))) as pool,
        ):
            now = time.monotonic()
            futures = {
                pool.submit(_ask, client, s, SEATS[s]["model"], SEATS[s]["lens"], text, max_tokens,
                            now + _seat_budget(s, timeout)): s
                for s in chosen
            }
            results: list[SeatResult] = []
            for fut in concurrent.futures.as_completed(futures):
                seat = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    results.append(SeatResult(
                        seat, SEATS[seat]["model"], None, None, f"{type(exc).__name__}: {exc}",
                        "absent", Status.ABSENT, True, _CANONICAL_SEAT_FAMILY.get(seat, "absent"),
                    ))
            order = {s: i for i, s in enumerate(chosen)}
            results.sort(key=lambda r: order.get(r.seat, 999))
            return results
    except SanctumError:
        raise
    except (httpx.TransportError, ssl.SSLError, OSError) as exc:
        raise NetworkError(
            f"could not reach the council at {url}: {type(exc).__name__}: {exc}",
            fix="confirm proxyd is up on :4040 (`sanctum doctor`) and --url is correct",
        ) from exc
    except Exception as exc:
        raise LocalError(
            f"council fan-out failed: {type(exc).__name__}: {exc}",
            fix="this is a CLI bug — re-run with --traceback and report it",
        ) from exc


def brainstorm_command(
    topic: Annotated[str | None, typer.Argument(help="Topic to brainstorm. Omit to read stdin.")] = None,
    file: Annotated[Path | None, typer.Option("--file", "-f", help="Read the topic from a file.")] = None,
    seats: Annotated[
        str | None, typer.Option("--seats", "-s", help="Comma list to subset seats (default: all seven).")
    ] = None,
    url: Annotated[str, typer.Option("--url", help="proxyd base URL.", envvar="SANCTUM_COUNCIL_URL")] = DEFAULT_URL,
    max_tokens: Annotated[int, typer.Option("--max-tokens", "-t", help="Per-seat response cap.", min=1)] = 900,
    timeout: Annotated[int, typer.Option("--timeout", help="Per-seat timeout ceiling (seconds).", min=1)] = 240,
    cacert: Annotated[Path, typer.Option("--cacert", help="CA to verify proxyd's TLS chain.")] = DEFAULT_CACERT,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON instead of panels.")] = False,
    min_families: Annotated[
        int, typer.Option("--min-families", min=0, help="Warn/fail below N distinct own-model families; 0=auto.")
    ] = 0,
    strict: Annotated[
        bool, typer.Option("--strict", help="Treat a duplicate-family fallback as a lost voice; exit 2 below floor.")
    ] = False,
) -> None:
    """Convene the heterogeneous council and print each Jedi's take in parallel."""
    haus_required("council")
    text = _resolve_topic(topic, file)
    chosen = _select_seats(seats)

    verify = _ssl_verify(cacert)
    is_loopback = url.startswith(("https://127.0.0.1", "https://localhost", "http://127.0.0.1", "http://localhost"))
    if verify is False and not is_loopback:
        err_console.print(
            f"[yellow]warning:[/] CA {cacert} not found — TLS verification disabled for {url}. "
            "Point --cacert at the Sanctum CA to verify the chain."
        )

    tel = _load_telemetry()
    span_cm: Any = telemetry.Span(tel, command="brainstorm") if tel is not None else contextlib.nullcontext()
    with span_cm as span:
        results = _summon(url, verify, chosen, text, max_tokens, float(timeout))
        div = _assess_diversity(results, chosen)
        floor = min_families or min(MIN_FAMILIES, len(div.designed)) or 1
        effective = div.achieved if strict else (div.achieved | (div.degraded_families & div.designed))
        diversity_ok = len(effective) >= floor
        lost = sorted(div.designed - div.achieved)
        if span is not None:
            span.set(prompt=text, intent="brainstorm", extra={
                "seats": list(chosen),
                "answered_seats": div.answered_seats,
                "designed_families": sorted(div.designed),
                "achieved_families": sorted(div.achieved),
                "effective_families": sorted(effective),
                "degraded_seats": list(div.degraded_seats),
                "absent_seats": list(div.absent_seats),
                "floor": floor,
                "diversity_ok": diversity_ok,
            })

    answered = [r for r in results if r.content]
    if not answered:
        errs = "; ".join(f"{r.seat}: {r.error}" for r in results if r.error)
        if any(_is_connect_error(r.error) for r in results):
            raise NetworkError(f"could not reach the council at {url} ({errs})",
                               fix="confirm proxyd is up on :4040 (`sanctum doctor`) and --url is correct")
        raise ProviderError(f"no council seat responded ({errs})",
                            fix="check proxyd :4040 seat routing and the *_MODEL env overrides")

    if json_output:
        console.print_json(data={
            "topic": text,
            "seats": [{
                "seat": r.seat, "model_attempted": r.model_attempted, "model_used": r.model_used,
                "family": r.family, "status": r.status.value, "degraded": r.degraded,
                "fallback_from": r.fallback_from, "response": r.content, "error": r.error,
            } for r in results],
            "diversity": {
                "designed_families": sorted(div.designed),
                "achieved_families": sorted(div.achieved),
                "degraded_families": sorted(div.degraded_families),
                "effective_families": sorted(effective),
                "floor": floor, "ok": diversity_ok, "strict": strict,
                "answered_seats": div.answered_seats, "total_seats": div.total_seats,
                "absent_seats": list(div.absent_seats), "degraded_seats": list(div.degraded_seats),
                "redundant_families": div.redundant, "lost_designed_families": lost,
            },
        })
    else:
        for r in results:
            console.print(_seat_panel(r, div.redundant))

    _emit_diversity(div, effective, floor, diversity_ok, lost)

    if strict and not diversity_ok:
        raise ProviderError(
            f"council homogenized: {len(effective)} distinct families < floor {floor}",
            fix="bring a degraded/absent family back (check `sanctum doctor`), lower --min-families, "
                "or drop --strict to accept degraded heterogeneity",
        )
