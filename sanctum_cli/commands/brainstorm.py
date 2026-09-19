"""``sanctum brainstorm "<topic>"`` — convene the heterogeneous Jedi Council.

Fans a topic out to the council seats and prints every Jedi's take, so the
operator can pressure-test a plan against distinct model families at once. Per
the neurodiversity doctrine, a single model in seven robes is not a council — so
this command treats **model family** as a first-class value and makes any
collapse toward homogeneity *impossible to miss*:

  - Each seat is tagged with its resolved family; diversity is counted in
    FAMILIES, never seats (7 seats all answering as Qwen = 1 family = homogenized).
  - A fallback / degradation / absence can never masquerade as a healthy voice:
    degraded seats render with a loud badge, absent seats in red with the verbatim
    error, and a council-summary footer states families AND seat-liveness.
  - Losing any DESIGNED family (e.g. Gemini dies) always surfaces a notice, even
    if the family floor is still met.
  - An answer is only an answer when it is COMPLETE and came from the seat's OWN
    family: a reply cut at the token cap is ``truncated`` and one that proxyd
    served from another family (a fallback rung, a hosted model) is ``diverted``.
    Neither is counted as that seat's voice.

Roster (aligned with OpenClaw agents + proxyd):
  Yoda=max-thinking (Fable), Windu=spacial (Gemini), Qui-Gon=code (Glimmer :3301),
  Mundi=finance (Grok), Cilghal=heretic (27B :6669), Jocasta+Mothma=brain (Opus 5).

Seats route through the house smart-router *proxyd* (``:4040``), which owns auth
and per-seat backend routing. A seat whose model fails or returns empty degrades
to the always-on local Qwen fallback — but only as a flagged last resort, and a
duplicate family is never counted as new diversity.

Scheduling follows the backends, not the roster (2026-09-19 incident, where
every Yoda attempt and most Cilghal attempts died to the CLIENT's own clock):

  - Each seat waits in a backend LANE. Its budget is sized to what proxyd will
    itself spend on that seat before giving up (council-finance 300 s; council-code,
    council-local-think and the bridge seats' TTFB 600 s ...) — asking for less
    guarantees a ReadTimeout on a request the server is still working. By default
    there is no operator ceiling (``--timeout 0``).
  - The on-box backends (the cathedral :1337/:6669 and the code seat :3301) each
    generate one request at a time, and proxyd's ladders move requests between
    them (council-code -> council-mlx, council-heretic -> council-local-think ->
    council-code), so they are ONE serialisation domain: the client never has two
    of its own requests in flight there. Everything else runs in parallel.
  - Within one run a timeout is never re-sent: on an on-box backend the first
    request keeps generating after a disconnect, so a re-send only queues behind
    it. ``--repoll`` (an explicit opt-in) re-asks a timed-out seat with DOUBLE the
    budget it lost, and never re-asks a seat proxyd /health reports unhealthy
    (it would only be substituted again).
  - A transport drop is retried ONCE, and only when ``/health`` shows the tunnel
    actually went down and came back (the MBP tunnel watchdog restarting the ssh
    tunnel). A drop while ``/health`` answers at once is proxyd closing the
    response itself — a server-side abort, not re-sent.
  - The heretic seat is STREAMED by default: proxyd's council-heretic entry has no
    read_timeout_secs, so a non-streamed request dies at proxyd's shared 120 s
    reqwest read timeout; a streamed one carries the cathedral's 15 s keep-alives.

proxyd serves a single TLS front door (PQC) on :4040 with a cert issued by the
Sanctum mTLS Root CA (``CN=sanctum-mlx``, not the access host), so we verify the
chain against ``~/.sanctum/certs/ca.crt`` with hostname binding off — strictly
safer than disabling verification.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import enum
import json
import math
import os
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from sanctum_cli import config, telemetry
from sanctum_cli.errors import LocalError, NetworkError, ProviderError, SanctumError, UserError
from sanctum_cli.haus import haus_required

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sanctum_cli.config import Telemetry as TelemetryConfig

console = Console()
err_console = Console(stderr=True)

DEFAULT_URL = os.environ.get("SANCTUM_COUNCIL_URL", "https://127.0.0.1:4040")
DEFAULT_CACERT = Path(
    os.environ.get("SANCTUM_COUNCIL_CACERT", str(Path.home() / ".sanctum/certs/ca.crt"))
)
FALLBACK_MODEL = os.environ.get("COUNCIL_FALLBACK_MODEL", "council-mlx")
CHAT_PATH = "/v1/chat/completions"
HEALTH_PATH = "/health"


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
# The patterns also classify the model a backend REPORTS it served (the response's
# ``model`` field): "Qwen3.8-27B-4bit-champion-ablated" is the heretic, and
# "Muse-Glimmer-30B-4bit" is the code seat's own model on :3301.
_FAMILY_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("council-max-thinking", "council-brain", "council-ops", "max-thinking", "opus", "sonnet", "haiku", "claude"), "claude"),
    (("council-heretic", "cilghal-health", "heretic", "ablated"), "heretic"),
    (("gemini", "council-spacial", "council-secure"), "gemini"),
    (("codestral", "council-code", "council-devstral", "devstral", "mistral", "glimmer"), "codestral"),
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
# Yoda=Fable-max, Windu=Gemini, Qui-Gon=Glimmer, Mundi=Grok, Cilghal=Heretic,
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


# ─── Lanes: the backend QUEUE a seat's request actually waits in ───
# bridge    = Claude Max bridge :3456 (claude CLI per request; kills a run at 300 s)
# agy       = Gemini agy-proxy :6543
# grok      = grok-oauth-proxy :4200 -> api.x.ai
# cathedral = sanctum-mlx :1337 AND its ablation port :6669 (ONE process, ONE FIFO)
# code      = sanctum-mlx :3301 (Glimmer), its own single generation lane
_LANE_BY_MODEL: dict[str, str] = {
    "council-max-thinking": "bridge",
    "council-brain": "bridge",
    "council-ops": "bridge",
    "claude-cli-offline": "bridge",
    "council-spacial": "agy",
    "council-finance": "grok",
    "council-heretic": "cathedral",
    "cilghal-health": "cathedral",
    "council-mlx": "cathedral",
    "council-local-think": "cathedral",
    "council-agents": "cathedral",
    "council-code": "code",
    "council-devstral": "code",
}
_LANE_BY_FAMILY: dict[str, str] = {
    "claude": "bridge",
    "gemini": "agy",
    "grok": "grok",
    "heretic": "cathedral",
    "qwen": "cathedral",
    "codestral": "code",
}
for _pair in os.environ.get("COUNCIL_LANE_MAP", "").split(","):
    if "=" in _pair:
        _k, _v = _pair.split("=", 1)
        _LANE_BY_MODEL[_k.strip()] = _v.strip()

# Lanes whose server can generate only ONE request at a time: the client never has
# two of its own requests in flight there (a second one would only queue behind the
# first and cannot be cancelled by a disconnect).
SERIAL_LANES = frozenset(
    s.strip() for s in os.environ.get("COUNCIL_SERIAL_LANES", "cathedral,code").split(",") if s.strip()
)
# Lanes with a local (on-box) backend: a hosted model answering for one of these
# means the prompt LEFT the box under a local seat's name.
_LOCAL_LANES = frozenset({"cathedral", "code"})
# Serial on-box lanes share ONE lock: proxyd's ladders move a request between them
# (council-code -> council-mlx on :1337; council-heretic -> council-local-think on
# :1337 -> council-code on :3301), so the lane a request is ADDRESSED to does not
# say which backend it will occupy.
# What the client CANNOT serialise: a REMOTE seat landing on the cathedral through its
# ladder's last rungs (council-finance -> ... council-mlx) or proxyd forcing agent
# "unknown" local after its daily USD cap. Neither is visible before the request;
# the answer that comes back is marked diverted.
_ON_BOX_LOCK = "on-box"


def _lock_key(lane: str) -> str | None:
    """The lock a request in ``lane`` must hold (None = none: a parallel lane)."""
    if lane not in SERIAL_LANES:
        return None
    return _ON_BOX_LOCK if lane in _LOCAL_LANES else lane


# Seats streamed by default (``--stream``/``--no-stream`` override): proxyd routes that
# have NO per-seat read_timeout_secs on an on-box backend. proxyd's shared reqwest
# client has a 120 s read (inactivity) timeout; a non-streamed cathedral request sends
# no byte until it is done, so it dies there and proxyd serves the fallback rung
# (council-heretic -> council-local-think: the NON-ablated model). A streamed one gets
# the cathedral's role chunk at once and a keep-alive every 15 s, queue included
# (sanctum-mlx server.rs PREFILL_KEEPALIVE_SECS), so that clock never fires.
STREAM_MODELS = frozenset(
    s.strip() for s in os.environ.get("COUNCIL_STREAM_MODELS", "council-heretic").split(",") if s.strip()
)

# Per-lane seat budget (seconds): sized to what PROXYD will itself spend on the
# seat before giving up, plus a margin, so the client is never the first to quit
# on a request that is still being worked (Mini proxyd config.yaml, read 2026-09-19):
#   bridge     620  council-max-thinking / council-brain ttfb 600, read 900. The
#                   bridge's own 300 s kill (manager.js DEFAULT_TIMEOUT) starts only
#                   AFTER a subprocess slot frees (FIFO, CLAUDE_MAX_SUBPROCESS_
#                   CONCURRENCY default 12), so 300 s + slot wait, bounded by proxyd.
#   grok       310  council-finance read/ttfb 300
#   agy        150  council-spacial has no per-seat timeout -> shared 120 s read
#   cathedral  620  council-mlx / council-local-think read/ttfb 600. council-heretic
#                   has NO read_timeout_secs -> shared 120 s: see STREAM_MODELS.
#   code       620  council-code read/ttfb 600
# The operator --timeout ceiling (default 0 = none) clips every lane but `bridge`;
# `--seat-timeout N` sets every seat's budget exactly.
# Env: SANCTUM_COUNCIL_TIMEOUT_<LANE> (e.g. ..._GROK=400).
_LANE_TIMEOUT_DEFAULTS: dict[str, float] = {
    "bridge": 620.0,
    "grok": 310.0,
    "agy": 150.0,
    "cathedral": 620.0,
    "code": 620.0,
    "other": 240.0,
}
_CEILING_EXEMPT_LANES = frozenset({"bridge"})


def _lane_timeouts(env: Mapping[str, str]) -> dict[str, float]:
    """Lane budgets with SANCTUM_COUNCIL_TIMEOUT_<LANE> overrides applied (a bad value
    is ignored rather than crashing the command)."""
    out = dict(_LANE_TIMEOUT_DEFAULTS)
    for lane in list(out):
        raw = env.get(f"SANCTUM_COUNCIL_TIMEOUT_{lane.upper()}")
        if raw:
            with contextlib.suppress(ValueError):
                val = float(raw)
                if val > 0:
                    out[lane] = val
    return out


LANE_TIMEOUTS = _lane_timeouts(os.environ)
SEAT_TIMEOUT_FLOOR = 30.0


# ─── Voice-preservation tuning (env-overridable where noted) ───
THINKING_SEATS = frozenset(
    s.strip() for s in os.environ.get("COUNCIL_THINKING_SEATS", "Windu").split(",") if s.strip()
)
THINKING_BUDGET_FLOOR = int(os.environ.get("COUNCIL_THINKING_FLOOR", "3072"))
THINKING_BUDGET_ESCALATED = int(os.environ.get("COUNCIL_THINKING_ESCALATED", "6144"))
# 429/503/504 are the classic transient answers: one bounded, backed-off retry each.
# NOT 502: on /v1/chat/completions proxyd returns 502 only after walking the seat's
# WHOLE ladder ("ALL SEATS FAILED ...", proxy.rs) and paging Force Flow — a retry
# re-walks every rung (cathedral included) and pages again.
_TRANSIENT_STATUS = frozenset({429, 503, 504})
RETRY_BACKOFF_CAP_S = float(os.environ.get("COUNCIL_RETRY_BACKOFF_CAP_S", "5"))
# httpx applies ONE scalar to connect, read, write and pool alike; split them so a
# dead tunnel fails in seconds while a long generation gets the whole seat budget.
CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 30.0
POOL_TIMEOUT_S = 10.0
HEALTH_PROBE_TIMEOUT_S = 10.0
HEALTH_POLL_S = 5.0
# How long to wait for /health after a transport drop. The MBP tunnel watchdog
# brings the ssh tunnel back in ~60-80 s (proxy-tunnel-watchdog.log).
DROP_HEALTH_WAIT_S = float(os.environ.get("COUNCIL_DROP_HEALTH_WAIT_S", "120"))
MIN_RETRY_S = 30.0          # never start a retry / fallback with less budget than this
# --repoll re-asks a timed-out seat with double the budget it lost, up to this cap.
REPOLL_BUDGET_CAP_S = float(os.environ.get("COUNCIL_REPOLL_BUDGET_CAP_S", "1800"))
TRUNCATION_RETRY_FLOOR = 1200
TRUNCATION_RETRY_CAP = int(os.environ.get("COUNCIL_TRUNCATION_RETRY_CAP", "4096"))
LOAD_POLL_S = 60.0
# The heretic seat and council-local-think are the SAME process and weights on the
# cathedral; both report the resident id ("Qwen3.8-27B-4bit"). A qwen-reported answer
# on the heretic seat is therefore ambiguous and is settled by proxyd /health.
_SHARED_WEIGHTS: dict[str, frozenset[str]] = {"heretic": frozenset({"qwen"})}

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


class Outcome(enum.StrEnum):
    """Per-seat FINAL status. ``status`` keeps its three historical values (callers
    branch on them); ``outcome`` says precisely what happened."""

    ANSWERED = "answered"        # the seat's own family, complete — the only real answer
    FALLBACK = "fallback"        # the client's own Qwen fallback answered (degraded)
    DIVERTED = "diverted"        # proxyd served another family / a hosted model (degraded)
    TRUNCATED = "truncated"      # cut at the token cap — NOT an answer (text kept as partial)
    EMPTY = "empty"              # 200 with no content (incl. thinking starvation)
    TIMEOUT = "timeout"          # the CLIENT's budget expired; the server may still be working
    DROPPED = "dropped"          # the connection was severed mid-request, retry exhausted
    UNREACHABLE = "unreachable"  # could not connect to proxyd at all
    HTTP_ERROR = "http_error"    # proxyd answered non-2xx
    ERROR = "error"              # anything else


_ANSWER_OUTCOMES = frozenset({Outcome.ANSWERED, Outcome.FALLBACK, Outcome.DIVERTED})


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
    outcome: str = ""          # Outcome value; "" = derive from status (legacy constructors)
    served_model: str | None = None   # the model the backend REPORTED (response "model")
    finish_reason: str | None = None
    partial: str | None = None        # text of a truncated / cut answer (never `content`)
    provenance: str | None = None     # match | unreported | unrecognized | ambiguous | diverted
    note: str | None = None
    lane: str = ""
    budget_s: float | None = None
    elapsed_s: float | None = None
    attempts: int = 0
    round: int = 0             # 0 = first fan-out; N = re-poll round N


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
    truncated_seats: tuple[str, ...] = ()
    diverted_seats: tuple[str, ...] = ()
    own_voice_seats: tuple[str, ...] = ()   # outcome == answered


@dataclass(frozen=True)
class AskOptions:
    """Per-seat knobs handed from the scheduler to `_ask`."""

    stream: bool = False
    transport_retries: int = 1
    health_wait_s: float = DROP_HEALTH_WAIT_S
    truncation_retry: bool = True
    health: Mapping[str, Any] | None = None       # proxyd /health "seats" snapshot
    lane_locks: Mapping[str, threading.Semaphore] | None = None   # keyed by _lock_key
    held_lock: str | None = None                  # the lock key the caller already holds


@dataclass(frozen=True)
class RunOptions:
    """Run-wide knobs (CLI flags) for `_summon`."""

    stream: bool | None = None            # None = per seat (STREAM_MODELS); True/False = all
    concurrency: int = 0                  # 0 = lane-aware; N = at most N seats in flight
    seat_timeout: float | None = None     # exact per-seat budget override
    transport_retries: int = 1
    health_wait_s: float = DROP_HEALTH_WAIT_S
    truncation_retry: bool = True
    tokens_by_seat: Mapping[str, int] = field(default_factory=dict)


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


def _lane_of(model: str) -> str:
    if model in _LANE_BY_MODEL:
        return _LANE_BY_MODEL[model]
    return _LANE_BY_FAMILY.get(_family_of(model), "other")


def _seat_lane(seat: str) -> str:
    return _lane_of(SEATS[seat]["model"])


def _seat_streams(seat: str, stream: bool | None) -> bool:
    """Whether ``seat`` is read as an SSE stream: ``--stream``/``--no-stream`` decide for
    every seat; by default only the STREAM_MODELS seats (the heretic) stream."""
    if stream is not None:
        return stream
    return SEATS[seat]["model"] in STREAM_MODELS


def _seat_budget(seat: str, ceiling: float, override: float | None = None) -> float:
    """Per-seat wall-clock budget (whole attempt chain), sized by the seat's LANE.

    ``override`` (``--seat-timeout``) sets it exactly — it can RAISE as well as lower,
    on every lane. Otherwise the lane budget applies, clipped by the operator
    ``--timeout`` ceiling for every lane but the Max bridge; a ceiling <= 0 means none."""
    if override is not None and override > 0:
        return float(override)
    lane = _seat_lane(seat)
    base = LANE_TIMEOUTS.get(lane, LANE_TIMEOUTS["other"])
    if lane not in _CEILING_EXEMPT_LANES and ceiling > 0:
        base = min(base, ceiling)
    return max(SEAT_TIMEOUT_FLOOR, base)


def _http_timeout(read_s: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT_S, read=max(1.0, read_s), write=WRITE_TIMEOUT_S, pool=POOL_TIMEOUT_S
    )


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


@dataclass(frozen=True)
class _Reply:
    content: str
    finish_reason: str | None
    served_model: str | None
    reasoning: int
    complete: bool = True


class _BudgetExpiredError(Exception):
    """The seat's wall budget ran out while a stream was still open."""

    def __init__(self, partial: str) -> None:
        super().__init__("seat budget expired mid-stream")
        self.partial = partial


class _StreamCutError(Exception):
    """The stream CLOSED CLEANLY with neither a finish_reason nor [DONE] (or carried an
    error event): the backend stopped mid-answer. That is a server-side failure — a
    severed tunnel shows up as an httpx transport error instead — so it is not retried."""

    def __init__(self, partial: str, reason: str = "") -> None:
        super().__init__(reason or "stream ended before the answer finished (no finish_reason, no [DONE])")
        self.partial = partial


def _reply_from_json(data: dict[str, Any]) -> _Reply:
    choices = data.get("choices") or []
    first = choices[0] if choices else {}
    content = (first.get("message") or {}).get("content", "") or ""
    served = data.get("model")
    return _Reply(
        content=content,
        finish_reason=first.get("finish_reason"),
        served_model=served if isinstance(served, str) and served else None,
        reasoning=_reasoning_tokens(data),
    )


def _post_json(client: Any, body: dict[str, Any], read_s: float) -> _Reply:
    resp = client.post(CHAT_PATH, json=body, timeout=_http_timeout(read_s))
    resp.raise_for_status()
    return _reply_from_json(resp.json())


def _post_stream(client: Any, body: dict[str, Any], read_s: float, deadline: float) -> _Reply:
    """SSE read of one completion. proxyd passes the upstream stream through; the
    cathedral emits a keep-alive delta every 15 s during queue/prefill, so a long
    generation never looks idle — the seat's wall budget is enforced here, between
    events. A JSON reply to a stream request (proxyd's gap guard) is parsed as JSON."""
    with client.stream("POST", CHAT_PATH, json={**body, "stream": True},
                       timeout=_http_timeout(read_s)) as resp:
        if resp.status_code >= 400:
            resp.read()
            resp.raise_for_status()
        if "text/event-stream" not in resp.headers.get("content-type", ""):
            resp.read()
            return _reply_from_json(resp.json())
        parts: list[str] = []
        finish: str | None = None
        served: str | None = None
        usage: dict[str, Any] = {}
        done = False
        for raw in resp.iter_lines():
            if time.monotonic() > deadline:
                raise _BudgetExpiredError("".join(parts))
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                raise _StreamCutError("".join(parts), f"stream error event: {str(chunk['error'])[:200]}")
            if not served and isinstance(chunk.get("model"), str) and chunk["model"]:
                served = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                text = (choice.get("delta") or {}).get("content")
                if text:
                    parts.append(text)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        if not done and finish is None:
            raise _StreamCutError("".join(parts))
        return _Reply("".join(parts), finish, served, _reasoning_tokens({"usage": usage}))


def _probe_health(client: Any) -> dict[str, Any] | None:
    """One GET /health. Any HTTP answer means proxyd is reachable (it returns 503
    when every provider is unhealthy — that is still an answer). None = unreachable."""
    try:
        resp = client.get(HEALTH_PATH, timeout=_http_timeout(HEALTH_PROBE_TIMEOUT_S))
    except Exception:
        return None
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _await_health(client: Any, max_wait: float) -> tuple[bool, bool]:
    """Poll /health until proxyd answers or ``max_wait`` elapses (bounded twice:
    by the clock and by an iteration count, so a frozen clock cannot spin).
    Returns (answered, was_down): was_down = at least one probe failed first, i.e.
    the path to proxyd itself was broken (a tunnel restart), not just one response."""
    end = time.monotonic() + max(0.0, max_wait)
    was_down = False
    for _ in range(int(max(0.0, max_wait) // HEALTH_POLL_S) + 2):
        if _probe_health(client) is not None:
            return True, was_down
        was_down = True
        if time.monotonic() + HEALTH_POLL_S > end:
            return False, was_down
        _sleep(HEALTH_POLL_S)
    return False, was_down


def _seat_health(client: Any) -> dict[str, Any] | None:
    data = _probe_health(client)
    if data is None:
        return None
    seats = data.get("seats")
    return seats if isinstance(seats, dict) else {}


_PERMANENT_CONNECT_TOKENS = (
    "CERTIFICATE_VERIFY_FAILED", "nodename nor servname", "Name or service not known",
    "getaddrinfo", "No address associated",
)


def _provenance(
    seat_model: str, designed: str, lane: str, served: str | None, health: Mapping[str, Any] | None
) -> tuple[str, str | None]:
    """Did the answer come from the seat's own family? Returns (verdict, note)."""
    if not served:
        h = (health or {}).get(seat_model)
        if isinstance(h, dict) and h.get("healthy") is False:
            return "diverted", (f"the backend did not report which model answered, while proxyd "
                                f"/health reports {seat_model} unhealthy (error_rate "
                                f"{h.get('error_rate_pct')}%) — most likely a proxyd fallback")
        return "unreported", None
    fam = _family_of(served)
    if lane in _LOCAL_LANES and "/" in served:
        return "diverted", (f"proxyd served {served}, a HOSTED model, for a local seat — "
                            "the prompt left the box")
    if fam == designed:
        return "match", None
    if fam in _SHARED_WEIGHTS.get(designed, frozenset()):
        h = (health or {}).get(seat_model)
        if isinstance(h, dict) and h.get("healthy") is False:
            return "diverted", (f"served {served}: weights {seat_model} shares with its proxyd "
                                f"fallback, while proxyd /health reports {seat_model} unhealthy "
                                f"(error_rate {h.get('error_rate_pct')}%) — a fallback answer")
        return "ambiguous", (f"served {served}: weights {seat_model} shares with its proxyd "
                             "fallback — cannot prove which one answered")
    if _real_family(fam):
        return "diverted", f"proxyd served {served} ({fam}), not a {designed} model"
    if "/" in served:
        return "diverted", f"proxyd served {served} (hosted), not a {designed} model"
    return "unrecognized", None


@dataclass
class _Attempt:
    outcome: Outcome
    content: str | None = None
    error: str | None = None
    served_model: str | None = None
    finish_reason: str | None = None
    partial: str | None = None
    starved: bool = False
    ladder_tried_fallback: bool = False   # proxyd's 502 ladder already included FALLBACK_MODEL


def _ladder_tried(text: str, model: str) -> bool:
    """Did a proxyd 502 "ALL SEATS FAILED requested=… · [seat✗why · …]" try ``model``?"""
    with contextlib.suppress(ValueError, TypeError):
        text = json.dumps(json.loads(text), ensure_ascii=False)
    return "ALL SEATS FAILED" in text and f"{model}✗" in text


def _ask(
    client: httpx.Client,
    seat: str,
    model: str,
    lens: str,
    topic: str,
    max_tokens: int,
    deadline: float,
    opts: AskOptions | None = None,
) -> SeatResult:
    """Resolve one seat. ALWAYS returns a SeatResult — never raises (a raised _ask
    would nuke the whole fan-out). Chain: home model (thinking-floored, one same-model
    escalation on starvation, one bounded retry on transient status, one retry after
    a transport drop once /health answers, one larger-cap retry on truncation for
    non-serial lanes) -> Qwen fallback (flagged) -> ABSENT. Every non-home answer is
    flagged DEGRADED; no answer is ABSENT with the verbatim error preserved."""
    opts = opts or AskOptions()
    home_family = _CANONICAL_SEAT_FAMILY.get(seat, SEATS.get(seat, {}).get("family", _family_of(model)))
    designed = SEATS.get(seat, {}).get("family", _family_of(model))
    lane = _lane_of(model)
    started = time.monotonic()
    budget = max(0.0, deadline - started)
    attempts = 0

    def _remaining() -> float:
        return deadline - time.monotonic()

    def _try(candidate: str, tokens: int) -> _Attempt:
        """One candidate, with at most one transient-status retry, one empty-codestral
        retry and ``transport_retries`` post-drop retries. Never raises."""
        nonlocal attempts, deadline
        transient_used = False
        empty_retry_used = False
        drops = 0
        body = _body(candidate, lens, topic, tokens)
        while True:
            remaining = _remaining()
            if remaining <= 0:
                return _Attempt(Outcome.TIMEOUT, error=(
                    f"client budget expired: {budget:.0f}s (lane {lane}) was spent before "
                    f"{candidate} could be asked"))
            attempts += 1
            drop: BaseException
            try:
                reply = (_post_stream(client, body, remaining, deadline) if opts.stream
                         else _post_json(client, body, remaining))
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code if exc.response is not None else 0
                if code in _TRANSIENT_STATUS and not transient_used and _remaining() > 1:
                    transient_used = True
                    _sleep(_retry_after_seconds(exc))
                    continue
                full = ""
                with contextlib.suppress(Exception):
                    full = (exc.response.text or "").strip() if exc.response is not None else ""
                detail = full[:240]
                label = f"rate-limited ({code})" if code in (429, 503) else f"HTTP {code}"
                if code == 502 and "ALL SEATS FAILED" in full:
                    label = "HTTP 502 — proxyd walked this seat's whole ladder, every rung failed (not re-walked)"
                return _Attempt(Outcome.HTTP_ERROR, error=f"{label}{': ' + detail if detail else ''}",
                                ladder_tried_fallback=code == 502 and _ladder_tried(full, FALLBACK_MODEL))
            except _BudgetExpiredError as exc:
                return _Attempt(Outcome.TIMEOUT, partial=exc.partial or None, error=(
                    f"client budget expired mid-stream: no complete answer within {budget:.0f}s "
                    f"(lane {lane}); raise it with --seat-timeout or "
                    f"SANCTUM_COUNCIL_TIMEOUT_{lane.upper()}"))
            except httpx.TimeoutException as exc:
                if not isinstance(exc, httpx.ConnectTimeout):
                    # The CLIENT gave up; the server is still working (and on a
                    # single-lane backend will keep working). Never re-fire.
                    return _Attempt(Outcome.TIMEOUT, error=(
                        f"{type(exc).__name__}: client budget expired — {candidate} did not "
                        f"answer within {budget:.0f}s (lane {lane}); proxyd was still working. "
                        f"Raise it with --seat-timeout, --timeout 0 or "
                        f"SANCTUM_COUNCIL_TIMEOUT_{lane.upper()}"))
                drop = exc
            except _StreamCutError as exc:
                return _Attempt(Outcome.ERROR, partial=exc.partial or None, error=(
                    f"{candidate}: {exc} — the backend stopped mid-answer (server-side); "
                    "not retried"))
            except (httpx.NetworkError, httpx.RemoteProtocolError, ssl.SSLError) as exc:
                drop = exc
            except Exception as exc:
                return _Attempt(Outcome.ERROR, error=f"{type(exc).__name__}: {exc}")
            else:
                content = reply.content.strip()
                if content:
                    if reply.finish_reason == "length":
                        return _Attempt(Outcome.TRUNCATED, served_model=reply.served_model,
                                        finish_reason="length", partial=content, error=(
                                            f"truncated at the {tokens}-token cap "
                                            "(finish_reason=length) — not a complete answer"))
                    return _Attempt(Outcome.ANSWERED, content=content, served_model=reply.served_model,
                                    finish_reason=reply.finish_reason)
                if reply.reasoning > 0:   # thinking starvation — the load-bearing signal
                    return _Attempt(Outcome.EMPTY, served_model=reply.served_model, starved=True,
                                    finish_reason=reply.finish_reason,
                                    error=f"empty (thinking starvation, reasoning={reply.reasoning})")
                err = f"empty response from {candidate} (finish={reply.finish_reason})"
                # codestral empties ~1/3 of the time — one quick own-model retry
                if _family_of(candidate) == "codestral" and not empty_retry_used and _remaining() > 1:
                    empty_retry_used = True
                    _sleep(min(0.5, RETRY_BACKOFF_CAP_S))
                    continue
                return _Attempt(Outcome.EMPTY, served_model=reply.served_model,
                                finish_reason=reply.finish_reason, error=err)

            # ── transport drop: the connection died, the seat did not answer ──
            msg = f"{type(drop).__name__}: {drop}"
            never_connected = isinstance(drop, (httpx.ConnectError, httpx.ConnectTimeout))
            if any(tok in msg for tok in _PERMANENT_CONNECT_TOKENS):
                return _Attempt(Outcome.UNREACHABLE, error=msg)
            if drops < opts.transport_retries:
                drops += 1
                answered, was_down = _await_health(client, opts.health_wait_s)
                if answered and (was_down or never_connected):
                    # the path to proxyd broke and came back (a tunnel restart), or the
                    # request never reached it: not the seat's fault, and no server copy
                    # of it can be running — the retry gets a fresh budget
                    deadline = time.monotonic() + budget
                    continue
                if answered:
                    # /health answered at once: the path to proxyd was up, so proxyd (or
                    # its upstream, e.g. an error inside a raw stream passthrough) closed
                    # this response. A re-send would re-run the generation — not a drop.
                    return _Attempt(Outcome.ERROR, error=(
                        f"{msg} — proxyd closed the response while its /health answered at once "
                        "(the tunnel was up): a server-side abort, not a tunnel drop; not re-sent"))
            kind = Outcome.UNREACHABLE if never_connected else Outcome.DROPPED
            return _Attempt(kind, error=(
                f"{msg} — connection to proxyd severed, not a seat verdict (the MBP "
                f"proxy-tunnel-watchdog restarting the ssh tunnel does this); "
                f"{drops} retry(ies) after /health"))

    def _result(a: _Attempt, candidate: str, *, fallback: bool) -> SeatResult:
        elapsed = round(time.monotonic() - started, 1)
        common: dict[str, Any] = {
            "served_model": a.served_model, "finish_reason": a.finish_reason, "lane": lane,
            "budget_s": round(budget, 1), "elapsed_s": elapsed, "attempts": attempts,
        }
        if a.content is None:
            return SeatResult(seat, model, None, None, a.error, "absent", Status.ABSENT, True,
                              home_family, outcome=a.outcome.value, partial=a.partial, **common)
        want = FALLBACK_FAMILY if fallback else designed
        verdict, note = _provenance(candidate, want, _lane_of(candidate) if fallback else lane,
                                    a.served_model, opts.health)
        if verdict == "diverted":
            # an unreported model is NOT the candidate's family — never credit it one
            fam = _family_of(a.served_model) if a.served_model else "unknown:unreported"
            return SeatResult(seat, model, a.served_model, a.content, None, fam,
                              Status.DEGRADED, True, home_family, outcome=Outcome.DIVERTED.value,
                              provenance=verdict, note=note, **common)
        if fallback:
            return SeatResult(seat, model, candidate, a.content, None, FALLBACK_FAMILY,
                              Status.DEGRADED, True, home_family, outcome=Outcome.FALLBACK.value,
                              provenance=verdict, note=note, **common)
        return SeatResult(seat, model, model, a.content, None, _family_of(model), Status.OK, False,
                          None, outcome=Outcome.ANSWERED.value, provenance=verdict, note=note, **common)

    try:
        is_thinking = seat in THINKING_SEATS
        tokens = max(max_tokens, THINKING_BUDGET_FLOOR) if is_thinking else max_tokens

        # 1. home model
        a = _try(model, tokens)
        if a.content:
            return _result(a, model, fallback=False)

        # 2. same-model escalation on genuine thinking starvation (before any fallback)
        if a.starved and _remaining() > 5:
            tokens = max(THINKING_BUDGET_ESCALATED, tokens)
            a2 = _try(model, tokens)
            if a2.content:
                return _result(a2, model, fallback=False)
            a = a2 if a2.error else a

        # 3. truncation: one retry with room to finish — never on a serial (single-lane)
        #    backend, where it would re-run the whole generation on the queue.
        if (a.outcome is Outcome.TRUNCATED and opts.truncation_retry and lane not in SERIAL_LANES
                and _remaining() > MIN_RETRY_S):
            a2 = _try(model, min(max(2 * tokens, TRUNCATION_RETRY_FLOOR), TRUNCATION_RETRY_CAP))
            if a2.content:
                return _result(a2, model, fallback=False)
            if a2.outcome is Outcome.TRUNCATED or not a.partial:
                a = a2

        # 4. Qwen fallback — flagged DEGRADED, only if home is not already the fallback.
        #    Not after a truncation (that voice exists, it needs room), not after a
        #    DROPPED/UNREACHABLE home (the connection failed, not the seat — the fallback
        #    would go over the same broken path), not when proxyd's 502 ladder already
        #    tried the fallback model, and only with enough budget left to be worth a
        #    request on the shared on-box lane (whose lock it takes).
        if a.ladder_tried_fallback:
            a = dataclasses.replace(a, error=f"{a.error}; fallback {FALLBACK_MODEL} skipped "
                                             "(proxyd's ladder already tried it)")
        elif (model != FALLBACK_MODEL
                and a.outcome not in (Outcome.TRUNCATED, Outcome.DROPPED, Outcome.UNREACHABLE)
                and _remaining() > MIN_RETRY_S):
            fb_lane = _lane_of(FALLBACK_MODEL)
            fb_key = _lock_key(fb_lane)
            lock = (opts.lane_locks or {}).get(fb_key) if fb_key and fb_key != opts.held_lock else None
            got = lock.acquire(timeout=max(0.0, _remaining() - MIN_RETRY_S)) if lock else True
            if got:
                try:
                    f = _try(FALLBACK_MODEL, max_tokens)
                finally:
                    if lock:
                        lock.release()
                if f.content:
                    return _result(f, FALLBACK_MODEL, fallback=True)
                a = dataclasses.replace(a, error=f"{a.error}; fallback {FALLBACK_MODEL}: {f.error}")
            else:
                a = dataclasses.replace(a, error=f"{a.error}; fallback {FALLBACK_MODEL} skipped "
                                                 f"(on-box lane {fb_lane} busy)")

        # 5. no voice at all
        return _result(a, model, fallback=False)
    except Exception as exc:
        return SeatResult(seat, model, None, None, f"{type(exc).__name__}: {exc}",
                          "absent", Status.ABSENT, True, home_family, outcome=Outcome.ERROR.value,
                          lane=lane, attempts=attempts)


def _outcome_of(r: SeatResult) -> Outcome:
    if r.outcome:
        return Outcome(r.outcome)
    if r.status is Status.OK:
        return Outcome.ANSWERED
    if r.status is Status.DEGRADED:
        return Outcome.FALLBACK
    return Outcome.ERROR


def _rank(r: SeatResult) -> int:
    o = _outcome_of(r)
    if o is Outcome.ANSWERED:
        return 3
    if o in _ANSWER_OUTCOMES:
        return 2
    if o is Outcome.TRUNCATED:
        return 1
    return 0


def _prefer(old: SeatResult, new: SeatResult) -> SeatResult:
    """Keep the better of two results for one seat (ties go to the newer one)."""
    return new if _rank(new) >= _rank(old) else old


def _bumped_tokens(tokens: int) -> int:
    return min(max(2 * tokens, TRUNCATION_RETRY_FLOOR), TRUNCATION_RETRY_CAP)


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
    # an answer is the seat's OWN, complete voice; a stand-in (fallback/diverted) is not
    answered = sum(1 for r in results if r.content and _outcome_of(r) is Outcome.ANSWERED)

    fam_to_seats: dict[str, list[str]] = {}
    for c in chosen:
        f = SEATS.get(c, {}).get("family", "absent")
        if _real_family(f):
            fam_to_seats.setdefault(f, []).append(c)
    redundant = {f: s for f, s in fam_to_seats.items() if len(s) > 1}

    return Diversity(
        designed, achieved, degraded_families, absent_seats, degraded_seats, redundant, answered,
        len(chosen),
        truncated_seats=tuple(r.seat for r in results if _outcome_of(r) is Outcome.TRUNCATED),
        diverted_seats=tuple(r.seat for r in results if _outcome_of(r) is Outcome.DIVERTED),
        own_voice_seats=tuple(r.seat for r in results if _outcome_of(r) is Outcome.ANSWERED),
    )


# ─── rendering ───
def _seat_panel(r: SeatResult, redundant: dict[str, list[str]]) -> Panel:
    outcome = _outcome_of(r)
    if r.status is Status.ABSENT:
        lost = r.fallback_from or "?"
        if outcome is Outcome.TRUNCATED:
            body = (f"[yellow]TRUNCATED — {lost} answer cut at the token cap; NOT counted as an "
                    f"answer. Re-run with a larger --max-tokens (or --repoll).[/]\n"
                    f"{escape(r.error or '')}\n\n[dim]{escape(r.partial or '')}[/]")
            return Panel(body, title=f"[bold yellow][TRUNCATED] {r.seat}  ({lost})[/]",
                         title_align="left", border_style="yellow")
        body = f"[red]ABSENT ({outcome.value}) — {lost} voice lost.[/]\n{escape(r.error or 'no response')}"
        return Panel(body, title=f"[bold red][ABSENT] {r.seat}  ({lost})[/]", title_align="left", border_style="red")
    if r.status is Status.DEGRADED:
        lost = r.fallback_from or "?"
        if outcome is Outcome.DIVERTED:
            banner = (f"[yellow]DIVERTED — {lost} seat answered by {escape(r.model_used or '?')} "
                      f"({escape(r.family)}), not its own model.[/]")
            if r.note:
                banner += f"\n[yellow]{escape(r.note)}[/]"
        else:
            banner = f"[yellow]DEGRADED — {lost} voice unreachable; answered as {r.model_used} ({r.family}).[/]"
        if r.family == FALLBACK_FAMILY:
            banner += "\n[yellow]This DUPLICATES an existing family — no new diversity.[/]"
        body = f"{banner}\n\n{escape(r.content or '')}"
        return Panel(body, title=f"[bold yellow]{r.seat}  [DEGRADED {lost}->{escape(r.family)}][/]",
                     title_align="left", border_style="yellow")
    # OK — but flag a configured redundancy (e.g. Yoda+Mundi both claude)
    title = f"[bold]{r.seat}[/]  [dim]{r.model_used} · {r.family}[/]"
    body = escape(r.content or "")
    peers = [s for s in redundant.get(r.family, []) if s != r.seat]
    if peers:
        body = f"[dim]redundant — duplicates {r.family} (also: {', '.join(peers)}); adds no diversity.[/]\n\n{body}"
    if r.note:
        body = f"[dim]{escape(r.note)}[/]\n\n{body}"
    return Panel(body, title=title, title_align="left", border_style="cyan")


def _emit_seat_status(results: list[SeatResult]) -> None:
    """One line per seat to stderr: the final, precise status of every seat."""
    for r in results:
        o = _outcome_of(r)
        timing = (f"{r.elapsed_s:.0f}s/{r.budget_s:.0f}s" if r.elapsed_s is not None and r.budget_s
                  else "")
        bits = [f"{r.seat}: {o.value.upper()}", f"lane={r.lane}" if r.lane else "", timing,
                f"served={r.served_model}" if r.served_model else "",
                f"attempts={r.attempts}" if r.attempts else "",
                f"round={r.round}" if r.round else ""]
        line = "  ".join(b for b in bits if b)
        if o is not Outcome.ANSWERED and (r.error or r.note):
            line += f"  — {r.error or r.note}"
        style = "cyan" if o is Outcome.ANSWERED else ("yellow" if o in _ANSWER_OUTCOMES
                                                      or o is Outcome.TRUNCATED else "red")
        err_console.print(f"[{style}]seat {escape(line)}[/]")


def _emit_diversity(div: Diversity, effective: frozenset[str], floor: int, ok: bool, lost: list[str]) -> None:
    """One-line council summary to stderr (so --json stdout stays pure JSON)."""
    head = (f"Neurodiversity: {len(effective)}/{floor} families "
            f"(designed {len(div.designed)}: {', '.join(sorted(div.designed)) or '—'}; "
            f"answered {', '.join(sorted(div.achieved)) or '—'}) | "
            f"{div.answered_seats}/{div.total_seats} seats answered in their own voice")
    detail = []
    for s in div.degraded_seats:
        detail.append(f"{s} DIVERTED" if s in div.diverted_seats else f"{s} fell back")
    for s in div.absent_seats:
        detail.append(f"{s} TRUNCATED" if s in div.truncated_seats else f"{s} ABSENT")
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
        "RemoteProtocolError", "Server disconnected", "UNEXPECTED_EOF", "ReadError",
    ))


def _failure_fix(results: list[SeatResult]) -> str:
    """Fix text chosen by WHY the seats failed — not a blanket 'check routing'."""
    outcomes = {_outcome_of(r) for r in results}
    hints = []
    if Outcome.TIMEOUT in outcomes:
        hints.append("a seat outlived the CLIENT's budget — raise it (--seat-timeout N, --timeout 0, "
                     "SANCTUM_COUNCIL_TIMEOUT_<LANE>), wait for a calmer Mini (--wait-load), "
                     "or re-ask just the absent seats (--repoll N)")
    if outcomes & {Outcome.DROPPED, Outcome.UNREACHABLE}:
        hints.append("the connection to proxyd dropped (MBP proxy-tunnel-watchdog restart?) — "
                     "check `curl -k https://127.0.0.1:4040/health`, then --repoll N")
    if Outcome.TRUNCATED in outcomes:
        hints.append("answers were cut at the token cap — raise --max-tokens")
    if outcomes & {Outcome.DIVERTED, Outcome.FALLBACK}:
        hints.append("a stand-in answered for a seat whose own model failed (proxyd fallback rung or the "
                     "client's council-mlx) — check that seat's backend in proxyd /health")
    if outcomes & {Outcome.HTTP_ERROR, Outcome.EMPTY, Outcome.ERROR}:
        hints.append("check proxyd :4040 seat routing and the *_MODEL env overrides")
    return "; ".join(hints) or "check proxyd :4040 seat routing and the *_MODEL env overrides"


def _load_telemetry() -> TelemetryConfig | None:
    """Best-effort: brainstorm is usable without an instance.yaml (e.g. from a laptop
    over Tailscale), so a missing/invalid config disables telemetry, not the command."""
    try:
        return config.load().cli.telemetry
    except SanctumError:
        return None


def _warn_unhealthy(health: Mapping[str, Any] | None, chosen: list[str]) -> None:
    if not health:
        return
    for s in chosen:
        h = health.get(SEATS[s]["model"])
        if isinstance(h, dict) and h.get("healthy") is False:
            err_console.print(
                f"[yellow]proxyd /health: {SEATS[s]['model']} is unhealthy "
                f"(error_rate {h.get('error_rate_pct')}%) — {s}'s answer will likely come "
                f"from a stand-in and will not count as its own voice.[/]")


def _probe_seat_health(url: str, verify: ssl.SSLContext | bool) -> Mapping[str, Any] | None:
    """One fresh proxyd /health "seats" snapshot (None when proxyd is unreachable)."""
    try:
        with httpx.Client(base_url=url, timeout=_http_timeout(HEALTH_PROBE_TIMEOUT_S), verify=verify) as c:
            return _seat_health(c)
    except Exception:
        return None


# Outcomes that say the seat's MODEL failed (vs. the queue, the network or the cap):
# while proxyd /health reports that model unhealthy, re-asking only buys another stand-in.
_MODEL_FAILURE_OUTCOMES = frozenset({Outcome.DIVERTED, Outcome.FALLBACK, Outcome.HTTP_ERROR,
                                     Outcome.EMPTY, Outcome.ERROR})


def _repoll_plan(
    prev: SeatResult, health: Mapping[str, Any] | None, ceiling: float, run: RunOptions
) -> tuple[str | None, float | None]:
    """(skip_reason, seat_budget) for re-asking ``prev.seat`` in a --repoll round.

    A seat whose model proxyd reports unhealthy is skipped (it would be substituted
    again). A timed-out seat is re-asked with DOUBLE the budget it lost (capped): its
    first request may still be generating on an on-box backend, so a re-send into the
    same budget would queue behind it and fail the same way."""
    outcome = _outcome_of(prev)
    model = SEATS[prev.seat]["model"]
    h = (health or {}).get(model)
    if outcome in _MODEL_FAILURE_OUTCOMES and isinstance(h, dict) and h.get("healthy") is False:
        return (f"proxyd /health reports {model} unhealthy (error_rate {h.get('error_rate_pct')}%) — "
                "a re-ask would only be answered by a stand-in again"), None
    if outcome is Outcome.TIMEOUT:
        lost = prev.budget_s or _seat_budget(prev.seat, ceiling, run.seat_timeout)
        doubled = min(2 * lost, REPOLL_BUDGET_CAP_S)
        if doubled <= lost:
            return (f"its {lost:.0f}s budget already reached the re-poll cap "
                    f"({REPOLL_BUDGET_CAP_S:.0f}s, COUNCIL_REPOLL_BUDGET_CAP_S)"), None
        return None, doubled
    return None, run.seat_timeout


def _summon(
    url: str,
    verify: ssl.SSLContext | bool,
    chosen: list[str],
    text: str,
    max_tokens: int,
    timeout: float,
    run: RunOptions | None = None,
) -> list[SeatResult]:
    """Dispatch the chosen seats LANE-AWARE: parallel across lanes, serial within a
    single-lane backend (SERIAL_LANES), optionally capped at ``run.concurrency``
    seats in flight. A seat's budget starts when it is actually dispatched, so a
    seat queued behind a lane-mate does not lose its time waiting."""
    run = run or RunOptions()
    budgets = {s: _seat_budget(s, timeout, run.seat_timeout) for s in chosen}
    client_timeout = _http_timeout(max([*budgets.values(), SEAT_TIMEOUT_FLOOR]) + RETRY_BACKOFF_CAP_S + 5)
    try:
        with (
            httpx.Client(base_url=url, timeout=client_timeout, verify=verify) as client,
            concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(chosen))) as pool,
        ):
            health = _seat_health(client)
            _warn_unhealthy(health, chosen)
            locks: dict[str, threading.Semaphore] = {
                key: threading.BoundedSemaphore(1)
                for key in {_lock_key(lane) for lane in SERIAL_LANES} if key
            }
            gate = threading.BoundedSemaphore(run.concurrency) if run.concurrency > 0 else None

            def _one(seat: str) -> SeatResult:
                key = _lock_key(_seat_lane(seat))
                lane_lock = locks.get(key) if key else None
                with gate or contextlib.nullcontext(), lane_lock or contextlib.nullcontext():
                    opts = AskOptions(
                        stream=_seat_streams(seat, run.stream), transport_retries=run.transport_retries,
                        health_wait_s=run.health_wait_s, truncation_retry=run.truncation_retry,
                        health=health, lane_locks=locks, held_lock=key,
                    )
                    return _ask(client, seat, SEATS[seat]["model"], SEATS[seat]["lens"], text,
                                run.tokens_by_seat.get(seat, max_tokens),
                                time.monotonic() + budgets[seat], opts=opts)

            futures = {pool.submit(_one, s): s for s in chosen}
            results: list[SeatResult] = []
            for fut in concurrent.futures.as_completed(futures):
                seat = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    results.append(SeatResult(
                        seat, SEATS[seat]["model"], None, None, f"{type(exc).__name__}: {exc}",
                        "absent", Status.ABSENT, True, _CANONICAL_SEAT_FAMILY.get(seat, "absent"),
                        outcome=Outcome.ERROR.value,
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


# ─── Mini load gate (optional) ───
def _parse_loadavg(text: str) -> float | None:
    toks = text.replace("{", " ").replace("}", " ").split()
    try:
        return float(toks[0])
    except (IndexError, ValueError):
        return None


def _remote_load1(host: str) -> float | None:
    """load1 of ``host`` via ssh (argv list, no shell, BatchMode). None if unreadable."""
    try:
        cp = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
             "sysctl -n vm.loadavg"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_loadavg(cp.stdout) if cp.returncode == 0 else None


def _wait_for_load(
    host: str, limit: float, wait_max: float,
    probe: Callable[[str], float | None] | None = None,
) -> float:
    """Block until ``host``'s load1 < ``limit``; ProviderError after ``wait_max`` s."""
    probe = probe or _remote_load1
    end = time.monotonic() + max(0.0, wait_max)
    last: float | None = None
    for _ in range(math.ceil(max(0.0, wait_max) / LOAD_POLL_S) + 1):
        last = probe(host)
        if last is not None and last < limit:
            err_console.print(f"[dim]load gate open: {host} load1={last:.2f} < {limit:g}[/]")
            return last
        if time.monotonic() + LOAD_POLL_S > end:
            break
        shown = "unreadable" if last is None else f"{last:.2f}"
        err_console.print(f"[dim]waiting for load: {host} load1={shown} (need < {limit:g}); "
                          f"next check in {LOAD_POLL_S:.0f}s[/]")
        _sleep(LOAD_POLL_S)
    shown = "unreadable" if last is None else f"{last:.2f}"
    raise ProviderError(
        f"load gate never opened: {host} load1={shown}, needed < {limit:g} within {wait_max:.0f}s",
        fix="raise --wait-max, relax --wait-load, or drop the gate",
    )


def _seat_json(r: SeatResult) -> dict[str, Any]:
    """One seat row. ``response`` holds ONLY the seat's own, complete answer — the DF2
    senders (and anything else) count a non-empty ``response`` as "this seat answered".
    A stand-in's text (client fallback / proxyd diversion) is in ``degraded_response``;
    a cut answer's text is in ``partial_response``."""
    own = _outcome_of(r) is Outcome.ANSWERED
    return {
        "seat": r.seat, "model_attempted": r.model_attempted, "model_used": r.model_used,
        "family": r.family, "status": r.status.value, "degraded": r.degraded,
        "fallback_from": r.fallback_from, "response": r.content if own else None,
        "degraded_response": None if own else r.content, "error": r.error,
        "outcome": _outcome_of(r).value, "served_model": r.served_model,
        "finish_reason": r.finish_reason, "partial_response": r.partial,
        "provenance": r.provenance, "note": r.note, "lane": r.lane, "budget_s": r.budget_s,
        "elapsed_s": r.elapsed_s, "attempts": r.attempts, "round": r.round,
    }


def brainstorm_command(
    topic: Annotated[str | None, typer.Argument(help="Topic to brainstorm. Omit to read stdin.")] = None,
    file: Annotated[Path | None, typer.Option("--file", "-f", help="Read the topic from a file.")] = None,
    seats: Annotated[
        str | None, typer.Option("--seats", "-s", help="Comma list to subset seats (default: all seven).")
    ] = None,
    url: Annotated[str, typer.Option("--url", help="proxyd base URL.", envvar="SANCTUM_COUNCIL_URL")] = DEFAULT_URL,
    max_tokens: Annotated[int, typer.Option("--max-tokens", "-t", help="Per-seat response cap.", min=1)] = 900,
    timeout: Annotated[
        int, typer.Option("--timeout", min=0,
                          help="Per-seat timeout CEILING (s) for every lane but the Max bridge; "
                               "0 (default) = none: each seat waits as long as proxyd will.")
    ] = 0,
    cacert: Annotated[Path, typer.Option("--cacert", help="CA to verify proxyd's TLS chain.")] = DEFAULT_CACERT,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON instead of panels.")] = False,
    min_families: Annotated[
        int, typer.Option("--min-families", min=0, help="Warn/fail below N distinct own-model families; 0=auto.")
    ] = 0,
    strict: Annotated[
        bool, typer.Option("--strict", help="Treat a duplicate-family fallback as a lost voice; exit 2 below floor.")
    ] = False,
    seat_timeout: Annotated[
        int | None, typer.Option("--seat-timeout", min=1,
                                 help="Set EVERY seat's budget to N s (can raise or lower; all lanes).")
    ] = None,
    stream: Annotated[
        bool | None, typer.Option("--stream/--no-stream",
                                  help="Stream every seat / no seat. Default: only the heretic seat, whose "
                                       "proxyd route has the shared 120 s read ceiling.")
    ] = None,
    concurrency: Annotated[
        int, typer.Option("--concurrency", min=0,
                          help="Max seats in flight; 0 = lane-aware (serial within single-lane backends).")
    ] = 0,
    require_all: Annotated[
        bool, typer.Option("--require-all", help="Exit 2 unless EVERY seat answered, complete, in its own voice.")
    ] = False,
    repoll: Annotated[
        int, typer.Option("--repoll", min=0, help="Re-ask seats that did not answer, serially, up to N rounds.")
    ] = 0,
    repoll_wait: Annotated[
        int, typer.Option("--repoll-wait", min=0, help="Seconds to pause before each re-poll round.")
    ] = 30,
    wait_load: Annotated[
        float | None, typer.Option("--wait-load", min=0.0,
                                   help="Dispatch (and re-poll) only when the Mini's load1 < this.")
    ] = None,
    load_host: Annotated[
        str | None, typer.Option("--load-host", envvar="SANCTUM_COUNCIL_LOAD_HOST",
                                 help="ssh host (a NAME, e.g. manoir's MagicDNS) whose load1 --wait-load reads.")
    ] = None,
    wait_max: Annotated[
        int, typer.Option("--wait-max", min=0, help="Give up on --wait-load after N seconds.")
    ] = 3600,
) -> None:
    """Convene the heterogeneous council and print each Jedi's take."""
    haus_required("council")
    text = _resolve_topic(topic, file)
    chosen = _select_seats(seats)
    if wait_load is not None and not load_host:
        raise UserError("--wait-load needs --load-host (or SANCTUM_COUNCIL_LOAD_HOST)",
                        fix="pass the Mini by NAME, e.g. --load-host bert@manoir.<tailnet>.ts.net")

    verify = _ssl_verify(cacert)
    is_loopback = url.startswith(("https://127.0.0.1", "https://localhost", "http://127.0.0.1", "http://localhost"))
    if verify is False and not is_loopback:
        err_console.print(
            f"[yellow]warning:[/] CA {cacert} not found — TLS verification disabled for {url}. "
            "Point --cacert at the Sanctum CA to verify the chain."
        )

    run = RunOptions(stream=stream, concurrency=concurrency,
                     seat_timeout=float(seat_timeout) if seat_timeout else None)
    tokens_by_seat = dict.fromkeys(chosen, max_tokens)
    rounds_used = 0

    def _gate() -> None:
        if wait_load is not None and load_host:
            _wait_for_load(load_host, wait_load, float(wait_max))

    tel = _load_telemetry()
    span_cm: Any = telemetry.Span(tel, command="brainstorm") if tel is not None else contextlib.nullcontext()
    with span_cm as span:
        _gate()
        results = _summon(url, verify, chosen, text, max_tokens, float(timeout), run=run)
        for rnd in range(1, repoll + 1):
            pending = [r for r in results if _outcome_of(r) is not Outcome.ANSWERED]
            if not pending:
                break
            rounds_used = rnd
            _sleep(float(repoll_wait))
            health = _probe_seat_health(url, verify)
            plans = {r.seat: _repoll_plan(r, health, float(timeout), run) for r in pending}
            asked = [r for r in pending if plans[r.seat][0] is None]
            err_console.print(
                f"[bold]re-poll {rnd}/{repoll}: {', '.join(r.seat for r in asked) or 'nobody'} "
                f"— serially, one seat per call"
                + "".join(f"; {s} skipped ({why})" for s, (why, _) in plans.items() if why) + "[/]")
            for prev in pending:
                skip, budget = plans[prev.seat]
                idx = next(i for i, r in enumerate(results) if r.seat == prev.seat)
                if skip:
                    note = f"re-poll {rnd} skipped: {skip}"
                    if note not in (prev.error or ""):
                        results[idx] = dataclasses.replace(
                            prev, error="; ".join(e for e in (prev.error, note) if e))
                    continue
                _gate()
                if _outcome_of(prev) is Outcome.TRUNCATED:
                    tokens_by_seat[prev.seat] = _bumped_tokens(tokens_by_seat[prev.seat])
                seat_run = dataclasses.replace(run, concurrency=1, tokens_by_seat=tokens_by_seat,
                                               seat_timeout=budget)
                try:
                    new = _summon(url, verify, [prev.seat], text, max_tokens, float(timeout), run=seat_run)[0]
                except SanctumError as exc:
                    new = dataclasses.replace(prev, error=f"re-poll {rnd}: {exc.message}", round=rnd)
                new = dataclasses.replace(new, round=rnd)
                results[idx] = _prefer(results[idx], new)
            if not asked:
                break
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
                "outcomes": {r.seat: _outcome_of(r).value for r in results},
                "repoll_rounds": rounds_used,
                "floor": floor,
                "diversity_ok": diversity_ok,
            })

    if json_output:
        console.print_json(data={
            "topic": text,
            "seats": [_seat_json(r) for r in results],
            "diversity": {
                "designed_families": sorted(div.designed),
                "achieved_families": sorted(div.achieved),
                "degraded_families": sorted(div.degraded_families),
                "effective_families": sorted(effective),
                "floor": floor, "ok": diversity_ok, "strict": strict,
                "answered_seats": div.answered_seats, "total_seats": div.total_seats,
                "absent_seats": list(div.absent_seats), "degraded_seats": list(div.degraded_seats),
                "redundant_families": div.redundant, "lost_designed_families": lost,
                "truncated_seats": list(div.truncated_seats),
                "diverted_seats": list(div.diverted_seats),
                "own_voice_seats": list(div.own_voice_seats),
                "all_answered": len(div.own_voice_seats) == div.total_seats,
            },
            "run": {
                "stream": stream, "streamed": {s: _seat_streams(s, stream) for s in chosen},
                "concurrency": concurrency, "require_all": require_all,
                "repoll": repoll, "repoll_rounds_used": rounds_used,
                "budgets_s": {s: _seat_budget(s, float(timeout), run.seat_timeout) for s in chosen},
                "lanes": {s: _seat_lane(s) for s in chosen},
            },
        })
    else:
        for r in results:
            console.print(_seat_panel(r, div.redundant))

    _emit_seat_status(results)
    _emit_diversity(div, effective, floor, diversity_ok, lost)

    own_voice = [r for r in results if r.content and _outcome_of(r) is Outcome.ANSWERED]
    if not own_voice:
        errs = "; ".join(f"{r.seat}: {r.error}" for r in results if r.error)
        stand_ins = [r for r in results if r.content]
        if stand_ins:
            named = ", ".join(f"{r.seat}={_outcome_of(r).value} ({r.model_used})" for r in stand_ins)
            raise ProviderError(f"no council seat answered in its own voice — stand-ins only: {named}"
                                f"{' (' + errs + ')' if errs else ''}", fix=_failure_fix(results))
        if any(_outcome_of(r) in (Outcome.DROPPED, Outcome.UNREACHABLE) or _is_connect_error(r.error)
               for r in results):
            raise NetworkError(f"could not reach the council at {url} ({errs})",
                               fix="confirm proxyd is up on :4040 (`sanctum doctor`) and --url is correct; "
                                   + _failure_fix(results))
        raise ProviderError(f"no council seat responded ({errs})", fix=_failure_fix(results))

    if strict and not diversity_ok:
        raise ProviderError(
            f"council homogenized: {len(effective)} distinct families < floor {floor}",
            fix="bring a degraded/absent family back (check `sanctum doctor`), lower --min-families, "
                "or drop --strict to accept degraded heterogeneity",
        )

    if require_all:
        missing = [r for r in results if _outcome_of(r) is not Outcome.ANSWERED]
        if missing:
            named = "; ".join(f"{r.seat}={_outcome_of(r).value}" for r in missing)
            raise ProviderError(f"not every seat answered in its own voice: {named}",
                                fix=_failure_fix(missing))
