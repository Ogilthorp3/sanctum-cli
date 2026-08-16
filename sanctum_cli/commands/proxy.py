"""``sanctum proxy`` — manage the local provider proxies.

Wraps the same agent operations but scoped to the canonical provider-
proxy LaunchAgents. Adds an HTTP probe to confirm the upstream actually
serves /v1/models, not just that launchctl thinks the job is running.
"""

from __future__ import annotations

import time
from typing import Annotated, Literal

import httpx
import typer
from rich.console import Console
from rich.table import Table

from sanctum_cli import config
from sanctum_cli.commands import agent

console = Console()

# Canonical proxy mapping. Keys are the friendly names users see; values
# tie a LaunchAgent label to the HTTP base URL we probe.
KNOWN_PROXIES: dict[str, tuple[str, str]] = {
    "claude-cli-proxy": ("com.sanctum.claude-cli-proxy", "http://127.0.0.1:1234"),
    "sanctum-server": ("com.sanctum.server", "http://127.0.0.1:8900"),
    "lmstudio-bridge": ("com.sanctum.lmstudio-bridge", "http://127.0.0.1:1234"),
}

Target = Literal["all", "claude-cli-proxy", "sanctum-server", "lmstudio-bridge"]
HTTP_TIMEOUT_S = 2.0


def _probe(url: str) -> tuple[bool, int | None, str | None]:
    try:
        t0 = time.perf_counter_ns()
        r = httpx.get(f"{url}/v1/models", timeout=HTTP_TIMEOUT_S)
        latency = (time.perf_counter_ns() - t0) // 1_000_000
        if r.status_code == 200:
            return True, int(latency), None
        return False, int(latency), f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        return False, None, str(exc)[:80]


def _agent_status(label: str) -> str:
    for r in agent._launchctl_list():
        if r.label == label:
            return r.status
    return "missing"


def proxy_status(
    target: Annotated[
        str,
        typer.Argument(
            help="Which proxy: all | claude-cli-proxy | sanctum-server | lmstudio-bridge."
        ),
    ] = "all",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Probe each proxy's LaunchAgent state + HTTP /v1/models endpoint."""
    if target == "all":
        names = list(KNOWN_PROXIES.keys())
    else:
        if target not in KNOWN_PROXIES:
            from sanctum_cli.errors import UserError

            msg = f"unknown proxy {target!r}; expected one of: all, {', '.join(KNOWN_PROXIES)}"
            raise UserError(msg)
        names = [target]

    rows: list[dict[str, object]] = []
    for name in names:
        label, url = KNOWN_PROXIES[name]
        agent_state = _agent_status(label)
        ok, latency_ms, err = _probe(url)
        rows.append(
            {
                "name": name,
                "label": label,
                "url": url,
                "agent": agent_state,
                "http_ok": ok,
                "latency_ms": latency_ms,
                "detail": err,
            }
        )

    if json_output:
        import json as _json

        print(_json.dumps(rows, indent=2))
        return

    t = Table(title="proxies", show_header=True, header_style="bold")
    t.add_column("name")
    t.add_column("agent", justify="right")
    t.add_column("http", justify="right")
    t.add_column("latency", justify="right")
    t.add_column("detail")
    for r in rows:
        t.add_row(
            str(r["name"]),
            agent._color(r["agent"]),  # type: ignore[arg-type]
            "[green]ok[/]" if r["http_ok"] else "[red]down[/]",
            f"{r['latency_ms']} ms" if r["latency_ms"] is not None else "—",
            str(r["detail"] or ""),
        )
    console.print(t)


def proxy_restart(
    target: Annotated[
        str,
        typer.Argument(help="Which proxy: claude-cli-proxy | sanctum-server | lmstudio-bridge."),
    ],
) -> None:
    """Restart one proxy LaunchAgent."""
    from sanctum_cli.errors import UserError

    if target not in KNOWN_PROXIES:
        msg = f"unknown proxy {target!r}"
        raise UserError(msg)
    label, _ = KNOWN_PROXIES[target]
    agent.agent_restart(label)


def proxy_logs(
    target: Annotated[
        str,
        typer.Argument(help="Which proxy: claude-cli-proxy | sanctum-server | lmstudio-bridge."),
    ],
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow new lines.")] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Initial lines.", min=0)] = 50,
) -> None:
    from sanctum_cli.errors import UserError

    if target not in KNOWN_PROXIES:
        msg = f"unknown proxy {target!r}"
        raise UserError(msg)
    label, _ = KNOWN_PROXIES[target]
    agent.agent_logs(label, follow=follow, lines=lines)


# Reference cfg to silence unused-import warnings if wiring evolves
_ = config
