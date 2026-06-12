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


# ─── Executors ───────────────────────────────────────────────────────


def _run_sanctum_status(params: dict[str, object]) -> str:  # noqa: ARG001
    """Brief health summary — one line, brevity-gated."""
    from sanctum_cli import config
    from sanctum_cli.commands import doctor

    cfg = config.load()
    report = doctor.collect(cfg)
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
    # Clamp to the published maximum regardless of what the model passed.
    # raw_lines is object; coerce via str→int to satisfy mypy.
    n = min(int(str(raw_lines)) if raw_lines is not None else 50, 200)

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
            out_lines.extend(line.rstrip() for line in buf)
    return "\n".join(out_lines)


# ─── Registry ────────────────────────────────────────────────────────

REGISTRY: dict[str, CouncilTool] = {
    t.name: t
    for t in (
        CouncilTool(
            name="sanctum_status",
            description=(
                "Return a one-line health summary: overall status, agent count, "
                "provider count, repo count, and degraded/failed tally."
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
                "service log. Raises an error for unknown service names."
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
