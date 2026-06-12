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
from pathlib import Path

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
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
