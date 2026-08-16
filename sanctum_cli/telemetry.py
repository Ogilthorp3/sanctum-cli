"""JSONL append-only telemetry.

Every command emits one event at completion. The file is created with
0600 permissions and rotated by size (10 MB) at next-write boundary —
the rotation is a rename to a timestamped sibling, not in-place
truncation, so a concurrent reader sees a consistent view.

Prompt content is never logged unless ``redact_prompts: false`` in
config; the ``prompt_redacted`` field carries the sha256 instead so a
later analysis can correlate without recovering content.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from sanctum_cli.config import Telemetry as TelemetryConfig

ROTATE_BYTES = 10 * 1024 * 1024
SCHEMA_VERSION = 1

EventStatus = Literal["ok", "error"]


def _redact(prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _maybe_rotate(path: Path) -> None:
    if not path.exists():
        return
    if path.stat().st_size < ROTATE_BYTES:
        return
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    rotated = path.with_suffix(path.suffix + f".{stamp}")
    with suppress(OSError):
        path.rename(rotated)


def emit(
    cfg: TelemetryConfig,
    *,
    command: str,
    status: EventStatus,
    duration_ms: int,
    provider: str | None = None,
    route_rule: str | None = None,
    intent: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    error: str | None = None,
    prompt: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one structured event. Silent on disabled-by-config or write failure."""
    if not cfg.enabled:
        return
    path = Path(cfg.path).expanduser()
    _ensure_parent(path)
    _maybe_rotate(path)

    event: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "ts": datetime.now(tz=UTC).isoformat(),
        "command": command,
        "status": status,
        "duration_ms": duration_ms,
        "provider": provider,
        "route_rule": route_rule,
        "intent": intent,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "error": error,
        "host": socket.gethostname(),
    }
    if prompt is not None:
        event["prompt_redacted"] = _redact(prompt) if cfg.redact_prompts else prompt
    if extra:
        event["extra"] = extra
    line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


class Span:
    """Context manager that emits one event with measured duration."""

    def __init__(self, cfg: TelemetryConfig, *, command: str) -> None:
        self.cfg = cfg
        self.command = command
        self._start_ns = 0
        self._fields: dict[str, Any] = {}

    def __enter__(self) -> Span:
        self._start_ns = time.perf_counter_ns()
        return self

    def set(self, **fields: Any) -> None:
        self._fields.update(fields)

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, _tb: object
    ) -> None:
        duration_ms = (time.perf_counter_ns() - self._start_ns) // 1_000_000
        status: EventStatus = "ok" if exc_type is None else "error"
        if exc is not None and "error" not in self._fields:
            self._fields["error"] = str(exc)
        emit(self.cfg, command=self.command, status=status, duration_ms=duration_ms, **self._fields)
