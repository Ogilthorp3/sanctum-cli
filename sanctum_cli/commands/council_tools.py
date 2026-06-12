"""Bounded typed tools for the council chamber.

The registry IS the contract: each tool wraps sanctum's own internals
behind a JSON schema, is classified read-or-mutate at registration, has
its output redacted before any model sees it, and writes an append-only
audit line before its result is returned. No free-form shell tool
exists, in any phase — the council's own unanimous ruling (fan-out,
2026-06-12). Phase 1 registers reads only; the mutate gate ships
dormant and tested, waiting for phase 2's verbs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

from sanctum_cli.secret_scanner import CONTENT_PATTERNS

AUDIT_LEDGER = Path.home() / ".sanctum" / "logs" / "council-tools-audit.jsonl"

# ~24KB ≈ 6k tokens — a tool_result is re-sent every later turn, so the
# budget is per-conversation, not per-call.
LOGS_TAIL_MAX_BYTES = 24_000


def redact(text: str) -> str:
    """Scrub known secret shapes before any model reads tool output.

    Reuses the backup gate's pattern list — one source of truth for what
    a secret looks like. Read-only is not safe-to-surface (council #9).
    """
    data = text.encode("utf-8", errors="replace")
    for name, pattern in CONTENT_PATTERNS:
        data = pattern.sub(f"[REDACTED:{name}]".encode(), data)
    return data.decode("utf-8", errors="replace")


def audit(
    ledger: Path,
    *,
    seat: str,
    session: str,
    tool: str,
    params: dict[str, object],
    kind: str,
    mode: str,
    outcome: str,
    duration_ms: int,
) -> None:
    """Append one audit line; the ledger is the durable record of every
    call, read and mutate, success and failure (council #5). Params are
    logged, outputs are not — the ledger must not leak what redaction
    might miss."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seat": seat,
        "session": session,
        "tool": tool,
        "params": params,
        "kind": kind,
        "mode": mode,
        "outcome": outcome,
        "duration_ms": duration_ms,
    }
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


# ─── Registry primitives ─────────────────────────────────────────────


@dataclass(frozen=True)
class CouncilTool:
    """One bounded instrument: schema in, text out, classified at birth."""

    name: str
    description: str
    input_schema: dict[str, object]
    kind: Literal["read", "mutate"]
    run: Callable[[dict[str, object]], str]


@dataclass(frozen=True)
class ToolResult:
    """Typed return value from run_tool — content plus an error flag."""

    content: str
    is_error: bool = False


def run_tool(
    name: str,
    params: dict[str, object],
    *,
    seat: str,
    session: str,
    ledger: Path | None = None,
    registry: dict[str, CouncilTool] | None = None,
) -> ToolResult:
    """Dispatch, time, audit, and redact one tool call.

    Resolution order for optional args defers to module-level defaults so
    that tests which monkeypatch AUDIT_LEDGER or REGISTRY see the right
    value at call time, not at import time.

    Fail-closed contract: if the audit line cannot be written, the tool
    call fails — an unaudited result must never reach a model.
    """
    resolved_ledger: Path = ledger if ledger is not None else AUDIT_LEDGER
    resolved_registry: dict[str, CouncilTool] = registry if registry is not None else REGISTRY

    t0 = time.perf_counter()

    if name not in resolved_registry:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        known = ", ".join(sorted(resolved_registry))
        msg = f"unknown tool: {name}; known: {known}"
        audit(
            resolved_ledger,
            seat=seat,
            session=session,
            tool=name,
            params=params,
            kind="unknown",
            mode="auto",
            outcome="error",
            duration_ms=duration_ms,
        )
        return ToolResult(content=msg, is_error=True)

    tool = resolved_registry[name]

    if tool.kind == "mutate":
        # phase 1 dispatch refuses mutations structurally — dormancy is
        # enforced here too, not only by what the registry contains
        duration_ms = int((time.perf_counter() - t0) * 1000)
        audit(
            resolved_ledger,
            seat=seat,
            session=session,
            tool=name,
            params=params,
            kind=tool.kind,
            mode="auto",
            outcome="error",
            duration_ms=duration_ms,
        )
        return ToolResult("mutate path not wired in phase 1", is_error=True)

    try:
        raw = tool.run(params)
        content = redact(raw)
        outcome = "ok"
        is_error = False
    except Exception as exc:  # executor errors become error results, not crashes
        content = redact(str(exc))
        outcome = "error"
        is_error = True

    duration_ms = int((time.perf_counter() - t0) * 1000)
    audit(
        resolved_ledger,
        seat=seat,
        session=session,
        tool=name,
        params=params,
        kind=tool.kind,
        mode="auto",
        outcome=outcome,
        duration_ms=duration_ms,
    )
    return ToolResult(content=content, is_error=is_error)


# ─── Mutate gate (dormant) ──────────────────────────────────────────


def mount_tools(
    allowed: tuple[str, ...],
    *,
    registry: dict[str, CouncilTool] | None = None,
    is_tty: bool,
) -> list[CouncilTool]:
    """The seat's allowlist intersected with what this session may hold.

    Mutations are simply not mounted when no human is at the REPL — a
    confirm gate with no one to confirm fails closed (council #8).
    """
    reg = REGISTRY if registry is None else registry
    tools = [reg[name] for name in allowed if name in reg]
    if not is_tty:
        tools = [t for t in tools if t.kind == "read"]
    return tools


def _read_confirmation(prompt: str) -> str:
    """Separated for tests; the REPL's stdin is the only authority."""
    return input(prompt)


def confirm_mutation(resolved_action: str) -> bool:
    """Show the RESOLVED action, not a paraphrase; only a literal y/Y
    proceeds. Enter is no. 'yes' is no. Ctrl-C is no. A closed stdin is
    no — every exit from this prompt that is not a literal yes declines
    (council #3, #7).

    Spec delta, recorded: the spec also demands timeout=no; a blocking
    prompt is still fail-closed (nothing fires while it waits), so the
    timeout lands with phase 2's verbs, where the input primitive may
    change anyway.
    """
    try:
        answer = _read_confirmation(f"⚠ mutation: {resolved_action} — proceed? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip() in ("y", "Y")


# ─── Executors ───────────────────────────────────────────────────────


def _run_sanctum_status(params: dict[str, object]) -> str:  # noqa: ARG001
    """Brief health summary — agents and providers only; no restic probes.

    Backups and repos are doctor's domain. Status must return in well
    under 5s; restic serial probes measured at 11.3s and must not run here.
    """
    from sanctum_cli import config
    from sanctum_cli.commands import doctor

    try:
        cfg = config.load()
    except Exception as exc:
        return f"config error: {exc}"
    agents = doctor._agents()
    providers = doctor._providers(cfg)
    report = doctor.Report(agents=agents, providers=providers, repos=[])
    return doctor.render_brief(report)


def _run_sanctum_doctor(params: dict[str, object]) -> str:  # noqa: ARG001
    """Full per-row health rows — agents, providers, and backup repos."""
    from sanctum_cli import config
    from sanctum_cli.commands import doctor

    cfg = config.load()
    report = doctor.collect(cfg)

    lines: list[str] = []
    if report.agents:
        lines.append("=== agents ===")
        for a in report.agents:
            lines.append(f"{a.label}  pid={a.pid}  last_exit={a.last_exit}  status={a.status}")
    if report.providers:
        lines.append("=== providers ===")
        for p in report.providers:
            lat = f"{p.latency_ms}ms" if p.latency_ms is not None else "-"
            detail = f"  {p.detail}" if p.detail else ""
            lines.append(f"{p.name}  latency={lat}  status={p.status}{detail}")
    if report.repos:
        lines.append("=== repos ===")
        for r in report.repos:
            detail = f"  {r.detail}" if r.detail else ""
            lines.append(f"{r.repo}  status={r.status}{detail}")
    lines.append(doctor.render_brief(report))
    return "\n".join(lines)


def _run_agent_list(params: dict[str, object]) -> str:  # noqa: ARG001
    """All com.sanctum.* LaunchAgents: label, pid, last_exit, status."""
    from sanctum_cli.commands.agent import _launchctl_list

    rows = _launchctl_list()
    if not rows:
        return "no com.sanctum.* agents loaded"
    lines = [f"{r.label}  pid={r.pid}  last_exit={r.last_exit}  status={r.status}" for r in rows]
    return "\n".join(lines)


def _run_logs_tail(params: dict[str, object]) -> str:
    """Tail the last N lines (≤200) of a named service log."""
    from sanctum_cli.commands.logs import LOG_MAP

    service = str(params.get("service", ""))
    raw_lines = params.get("lines", 50)
    # Coerce robustly: float/str/int all accepted; garbage → error result.
    try:
        n = max(1, min(int(float(str(raw_lines))), 200)) if raw_lines is not None else 50
    except (ValueError, TypeError) as exc:
        return f"invalid lines value {raw_lines!r}: {exc}"

    paths = LOG_MAP.get(service.lower())
    if not paths:
        known = ", ".join(sorted(LOG_MAP))
        raise ValueError(f"unknown service '{service}'; known: {known}")

    extant = [p for p in paths if p.is_file()]
    if not extant:
        return (
            f"no log files exist yet for {service} (expected: {', '.join(str(p) for p in paths)})"
        )

    out_lines: list[str] = []
    for p in extant:
        out_lines.append(f"── {p} ──")
        with p.open(encoding="utf-8", errors="replace") as fh:
            # Collect last n lines without loading the whole file into memory.
            buf: list[str] = []
            for line in fh:
                buf.append(line)
                if len(buf) > n:
                    buf.pop(0)
            if buf:
                out_lines.extend(line.rstrip() for line in buf)
            else:
                out_lines.append("(empty)")

    # Apply byte budget: drop OLDEST content lines until the joined output
    # fits within LOGS_TAIL_MAX_BYTES.  Header lines (── path ──) are kept
    # as structural anchors; only content lines are candidates for removal.
    content = "\n".join(out_lines)
    if len(content.encode("utf-8", errors="replace")) > LOGS_TAIL_MAX_BYTES:
        # Separate headers from content so we can trim content independently.
        headers: list[str] = [ln for ln in out_lines if ln.startswith("── ")]
        content_only: list[str] = [ln for ln in out_lines if not ln.startswith("── ")]
        dropped = 0
        while content_only:
            candidate = "\n".join(headers + content_only)
            if len(candidate.encode("utf-8", errors="replace")) <= LOGS_TAIL_MAX_BYTES:
                break
            content_only.pop(0)  # drop oldest content line
            dropped += 1
        trimmed = headers + content_only
        if dropped:
            trimmed.insert(0, f"[truncated: {dropped} older lines dropped to fit the tool budget]")
        return "\n".join(trimmed)

    return content


# ─── Registry ────────────────────────────────────────────────────────

REGISTRY: dict[str, CouncilTool] = {
    t.name: t
    for t in (
        CouncilTool(
            name="sanctum_status",
            description=(
                "Return a one-line health summary: overall status, agent count, "
                "provider count, and degraded/failed tally. "
                "Probes agents and providers only — backups and repos are "
                "sanctum_doctor's domain."
            ),
            input_schema={"type": "object", "properties": {}},
            kind="read",
            run=_run_sanctum_status,
        ),
        CouncilTool(
            name="sanctum_doctor",
            description=(
                "Return a full per-row health report: every com.sanctum.* "
                "LaunchAgent, every configured provider, and every backup repo, "
                "each with status and detail. More verbose than sanctum_status."
            ),
            input_schema={"type": "object", "properties": {}},
            kind="read",
            run=_run_sanctum_doctor,
        ),
        CouncilTool(
            name="agent_list",
            description=(
                "List all com.sanctum.* LaunchAgents currently known to launchctl, "
                "with their pid, last exit code, and computed status."
            ),
            input_schema={"type": "object", "properties": {}},
            kind="read",
            run=_run_agent_list,
        ),
        CouncilTool(
            name="logs_tail",
            description=(
                "Return the last N lines (default 50, max 200) from a named "
                "service log, capped at 24KB. Raises an error for unknown "
                "service names."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": (
                            "Service name (e.g. 'r2d2', 'proxyd', 'yoda'). "
                            "Use sanctum logs --list to enumerate."
                        ),
                    },
                    "lines": {
                        "type": "integer",
                        "maximum": 200,
                        "default": 50,
                        "description": "Number of tail lines to return (clamped to 200).",
                    },
                },
                "required": ["service"],
            },
            kind="read",
            run=_run_logs_tail,
        ),
    )
}
assert len(REGISTRY) == 4, "duplicate tool name collapsed the registry"
