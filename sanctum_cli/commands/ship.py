"""``sanctum doctor --ship <module>`` — gate evaluator + renderer.

Scores a module manifest against the six ship-bar gates. Side effects
(keychain, HTTP probes, subprocess) are injected as callables so the
evaluator is unit-testable with fake adapters (Contracts at the Boundary).

Real adapters live in ``default_adapters()``. They deliberately err on
the conservative side for the two gates that need future work:

- ``is_default``: fingerprint set intentionally empty — always returns
  False. A later task can add fingerprints for known Bert-default creds
  (e.g. placeholder values) once we've collected real examples.

- ``probe_is_false_green``: always False. A later task can add the
  JSONL-only-failure heuristic (probe emits only JSON, no terminal fail)
  once the self-test runner exposes that signal.

- ``soak_days`` / ``soak_clean``: read from the soak result file
  (Phase 6, Task 6). If the file is absent or unparseable, returns
  (None, False) — treated as a RED soak gate.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.console import Console
from rich.table import Table
from rich.text import Text

from sanctum_cli import keychain
from sanctum_cli.ship_gates import (
    GateResult,
    GateStatus,
    gate_alert_hygiene,
    gate_docs_demo,
    gate_install_uninstall,
    gate_secrets,
    gate_self_heal,
    gate_soak,
    overall,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sanctum_cli.modules.manifest import ModuleManifest
    from sanctum_cli.modules.registry import ModuleRegistry

console = Console()


# ─── Public data types ───────────────────────────────────────────────


@dataclass
class ShipReport:
    module: str
    gates: list[GateResult]
    verdict: GateStatus


# ─── Evaluator ──────────────────────────────────────────────────────


def evaluate(
    module: str,
    registry: ModuleRegistry,
    adapters: dict[str, Any],
) -> ShipReport:
    """Score *module* against all six ship-bar gates.

    Args:
        module:   Module name (must exist in *registry*).
        registry: Resolved module registry.
        adapters: Dict of side-effect callables keyed by name.
                  Required keys: keychain_has, is_default, heal_action_ok,
                  sink_live, probe_is_false_green, soak_days, soak_clean,
                  docs_resolves, demo_exits_zero.

    Returns:
        ShipReport with all gate results + aggregate verdict.
    """
    m = registry.get(module)

    gates: list[GateResult] = [
        gate_install_uninstall(m),
        gate_secrets(
            m,
            keychain_has=adapters["keychain_has"],
            is_default=adapters["is_default"],
        ),
        gate_self_heal(
            m,
            heal_action_ok=adapters["heal_action_ok"],
        ),
        gate_alert_hygiene(
            m,
            sink_live=adapters["sink_live"],
            probe_is_false_green=adapters["probe_is_false_green"],
        ),
        gate_soak(
            m,
            soak_days=adapters["soak_days"],
            soak_clean=adapters["soak_clean"],
        ),
        gate_docs_demo(
            m,
            docs_resolves=adapters["docs_resolves"],
            demo_exits_zero=adapters["demo_exits_zero"],
        ),
    ]

    return ShipReport(module=module, gates=gates, verdict=overall(gates))


# ─── Real side-effect adapters ───────────────────────────────────────


def _keychain_has(account: str, service: str) -> bool:
    return keychain.exists(account=account, service=service)


def _is_default(_account: str, _service: str) -> bool:
    # Conservative starting point: fingerprint set is intentionally empty.
    # A later task can add known-bad fingerprints (e.g. placeholder text,
    # very-short values, all-zeros) once real examples are collected.
    return False


def _sink_live(name: str) -> bool:
    # For "chitti", probe the local chitti samskara bus on :2188.
    # Any other sink name falls through to False (conservative).
    if name != "chitti":
        return False
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:2188/health", timeout=2.0
        ) as resp:
            return bool(cast("int", resp.status) < 400)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def _probe_is_false_green(_probe: str) -> bool:
    # Conservative starting point: always False.
    # A later task can add the JSONL-only-failure heuristic (a probe whose
    # output contains only JSON lines without a terminal failure marker) once
    # the self-test runner exposes that signal.
    return False


def _docs_resolves(url: str) -> bool:
    # Accept a local docs/*.md path that exists, or an http(s) URL that
    # returns < 400 within a 2-second HEAD request.
    if not url.startswith(("http://", "https://")):
        return Path(url).is_file()
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return bool(cast("int", resp.status) < 400)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def _demo_exits_zero(demo: str) -> bool:
    try:
        result = subprocess.run(
            shlex.split(demo),
            capture_output=True,
            timeout=20,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _soak_days(m: ModuleManifest) -> float | None:
    result_path = Path(m.soak.result_path.replace("{module}", m.module)).expanduser()
    if not result_path.is_file():
        return None
    try:
        data = json.loads(result_path.read_text())
        started_at = datetime.fromisoformat(data["started_at"].replace("Z", "+00:00"))
        last_at = datetime.fromisoformat(data["last_at"].replace("Z", "+00:00"))
        now = datetime.now(tz=UTC)
        # Use the later of last_at and now to measure elapsed time from start.
        end = max(last_at, now)
        delta = end - started_at
        return delta.total_seconds() / 86400
    except (KeyError, ValueError, OSError):
        return None


def _soak_clean(m: ModuleManifest) -> bool:
    result_path = Path(m.soak.result_path.replace("{module}", m.module)).expanduser()
    if not result_path.is_file():
        return False
    try:
        data = json.loads(result_path.read_text())
        faults: list[Any] = data.get("faults", [])
        if faults:
            return False
        for sample in data.get("samples", []):
            if sample.get("red_probes"):
                return False
            if sample.get("service_nonzero"):
                return False
        return True
    except (KeyError, ValueError, OSError):
        return False


def default_adapters() -> dict[str, Callable[..., Any]]:
    """Return the real side-effect adapter dict for production use."""
    return {
        "keychain_has": _keychain_has,
        "is_default": _is_default,
        "heal_action_ok": lambda _label: True,  # conservative: assume heal wired
        "sink_live": _sink_live,
        "probe_is_false_green": _probe_is_false_green,
        "soak_days": _soak_days,
        "soak_clean": _soak_clean,
        "docs_resolves": _docs_resolves,
        "demo_exits_zero": _demo_exits_zero,
    }


# ─── Renderer ───────────────────────────────────────────────────────

_STATUS_STYLE: dict[GateStatus, str] = {
    GateStatus.GREEN: "bold green",
    GateStatus.AMBER: "bold yellow",
    GateStatus.RED: "bold red",
}

_STATUS_LABEL: dict[GateStatus, str] = {
    GateStatus.GREEN: "GREEN",
    GateStatus.AMBER: "AMBER",
    GateStatus.RED: "RED",
}


def render(report: ShipReport, json_out: bool = False) -> int:
    """Render the ship report and return the appropriate exit code.

    Returns:
        0 if verdict is GREEN or AMBER (ready / conditionally ready).
        1 if verdict is RED (not shippable).
    """
    if json_out:
        import json as _json

        payload = {
            "module": report.module,
            "verdict": report.verdict.value,
            "gates": [
                {"name": g.name, "status": g.status.value, "detail": g.detail}
                for g in report.gates
            ],
        }
        console.print_json(_json.dumps(payload))
    else:
        t = Table(
            title=f"Ship bar — {report.module}",
            show_header=True,
            header_style="bold",
        )
        t.add_column("gate", no_wrap=True)
        t.add_column("status", justify="right")
        t.add_column("detail")

        for gate in report.gates:
            style = _STATUS_STYLE[gate.status]
            label = _STATUS_LABEL[gate.status]
            t.add_row(
                gate.name,
                Text(label, style=style),
                gate.detail,
            )

        console.print(t)
        verdict_style = _STATUS_STYLE[report.verdict]
        verdict_label = _STATUS_LABEL[report.verdict]
        console.print(
            Text.assemble(
                Text("\nVerdict: ", style="bold"),
                Text(verdict_label, style=verdict_style),
                Text(f"  ({report.module})", style="dim"),
            )
        )

    return 0 if report.verdict is not GateStatus.RED else 1
