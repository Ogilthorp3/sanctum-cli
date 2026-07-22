"""``sanctum doctor`` — health probes across LaunchAgents, providers, and repos.

Brevity-gated by default: prints a single OK line if everything checks out,
expands to per-row detail only when `--full` is passed *or* something is
DEGRADED/FAILED. Matches the Jedi briefing rule.
"""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

import typer
from rich.console import Console
from rich.table import Table

from sanctum_cli import config
from sanctum_cli.errors import LocalError
from sanctum_cli.providers import Provider, make_provider
from sanctum_cli.providers.base import HealthSnapshot

console = Console()
err = Console(stderr=True)

LAUNCHCTL_TIMEOUT_S = 5
RESTIC_PROBE_TIMEOUT_S = 10
PROVIDER_PROBE_TIMEOUT_S = 8

Status = Literal["OPERATIONAL", "REPORTING", "DEGRADED", "FAILED", "UNKNOWN"]

HTTP_PROBE_TIMEOUT_S = 2

# One-shot agents whose non-zero exit is a *designed signal* (drift detected,
# drill WARN, self-test findings) — not a failure of the agent itself. Rendered
# REPORTING (benign), never counted as degraded/failed. Triaged 2026-07-21:
# these fire non-zero by design and were drowning real failures in false red.
REPORTING_LABELS: frozenset[str] = frozenset(
    {
        "com.sanctum.council-drift",
        "com.sanctum.council-guardian",
        "com.sanctum.council-integrity",
        "com.sanctum.git-drift-sentinel",
        "com.sanctum.secrets-sync-drift-check",
        "com.sanctum.model-latest-check",
        "com.sanctum.resilience-test",
        "com.sanctum.post-boot",
        "com.sanctum.nightly-compactor",
    }
)


def _daemon_probes() -> dict[str, str]:
    """Long-lived daemons that may run outside launchd (started by another
    supervisor), or whose crash-looping launchd copy shadows a healthy peer.
    A live health probe is more truthful than launchctl label presence."""
    import os

    ff = os.environ.get("FORCE_FLOW_URL", "http://127.0.0.1:4077").rstrip("/")
    return {
        "com.sanctum.force-flow": f"{ff}/health",
        "com.sanctum.thalamus": "http://127.0.0.1:1988/status",
    }


def _http_ok(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=HTTP_PROBE_TIMEOUT_S) as r:
            return 200 <= getattr(r, "status", 200) < 400
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class AgentRow:
    label: str
    pid: str
    last_exit: str
    status: Status


@dataclass(frozen=True, slots=True)
class ProviderRow:
    name: str
    status: Status
    latency_ms: int | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class RepoRow:
    repo: str
    status: Status
    detail: str | None


@dataclass(frozen=True, slots=True)
class Report:
    agents: list[AgentRow]
    providers: list[ProviderRow]
    repos: list[RepoRow]

    @property
    def overall(self) -> Status:
        rows: Iterable[Status] = (
            *(a.status for a in self.agents),
            *(p.status for p in self.providers),
            *(r.status for r in self.repos),
        )
        worst: Status = "OPERATIONAL"
        for s in rows:
            worst = _worse(worst, s)
        return worst


# ─── Probes ─────────────────────────────────────────────────────────


def _agents() -> list[AgentRow]:
    expected_labels = ["com.sanctum.force-flow"]
    if not shutil.which("launchctl"):
        return []
    try:
        out = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=LAUNCHCTL_TIMEOUT_S,
            check=True,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return []

    rows: list[AgentRow] = []
    for raw in out.stdout.splitlines()[1:]:  # header
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        pid, last_exit, label = parts[0], parts[1], parts[2]
        if not label.startswith("com.sanctum."):
            continue
        rows.append(
            AgentRow(
                label=label,
                pid=pid,
                last_exit=last_exit,
                status=_agent_status(pid, last_exit, label),
            )
        )

    # Truth-over-launchd: force-flow/thalamus can run outside launchd, or a
    # crash-looping launchd duplicate can shadow a healthy peer that already
    # holds the port. A live health probe wins over the launchctl verdict.
    index = {r.label: i for i, r in enumerate(rows)}
    for label, url in _daemon_probes().items():
        if label in index:
            i = index[label]
            # Only ever upgrade: a live probe overrides a crash-looping/absent
            # launchd verdict. Never downgrade what launchctl reports healthy.
            if rows[i].status in ("FAILED", "DEGRADED") and _http_ok(url):
                rows[i] = replace(rows[i], status="OPERATIONAL")
        elif label in expected_labels:
            # An expected daemon missing from launchctl: trust the health probe.
            alive = _http_ok(url)
            rows.append(
                AgentRow(
                    label=label,
                    pid="-",
                    last_exit="-",
                    status="OPERATIONAL" if alive else "FAILED",
                )
            )

    rows.sort(key=lambda r: r.label)
    return rows


def _agent_status(pid: str, last_exit: str, label: str = "") -> Status:
    # launchctl list output: PID="-" if not running. Last exit -9 etc means killed.
    if pid not in ("-", "0"):
        return "OPERATIONAL"
    try:
        rc = int(last_exit)
    except ValueError:
        return "UNKNOWN"
    if rc == 0:
        return "OPERATIONAL"  # successful one-shot
    if rc == -15:
        # SIGTERM: launchd's normal way to stop an agent (schedule/throttle/
        # kickstart). Benign. NOT -9 (SIGKILL) — that can be OOM/watchdog and
        # stays DEGRADED so a force-kill still surfaces.
        return "OPERATIONAL"
    if label in REPORTING_LABELS:
        # Non-zero by design: monitor detected drift / drill reported WARN.
        return "REPORTING"
    return "DEGRADED"


def _provider(name: str, p: Provider) -> ProviderRow:
    snap: HealthSnapshot
    try:
        snap = p.health()
    except Exception as exc:  # provider.health() shouldn't raise but defend anyway
        snap = HealthSnapshot(
            ok=False, latency_ms=None, quota_remaining=None, detail=str(exc)[:160]
        )
    finally:
        # Built solely to probe; release its socket now instead of leaking the
        # pooled connection to GC (surfaces as a ResourceWarning under pytest).
        p.close()
    status: Status = "OPERATIONAL" if snap.ok else "FAILED"
    return ProviderRow(name=name, status=status, latency_ms=snap.latency_ms, detail=snap.detail)


def _providers(cfg: config.Config) -> list[ProviderRow]:
    """Probe all three providers in parallel, bounded by per-call timeouts."""
    names = ("claude", "gemini", "mlx_local")
    rows: list[ProviderRow] = []
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = {}
        for n in names:
            try:
                p = make_provider(n, cfg.cli.providers)
            except (LocalError, ImportError) as exc:
                detail = exc.message[:120] if hasattr(exc, "message") else str(exc)[:120]
                rows.append(
                    ProviderRow(
                        name=n,
                        status="UNKNOWN",
                        latency_ms=None,
                        detail=detail,
                    )
                )
                continue
            futures[pool.submit(_provider, n, p)] = n
        for f in as_completed(futures, timeout=PROVIDER_PROBE_TIMEOUT_S * len(names)):
            rows.append(f.result())
    rows.sort(key=lambda r: r.name)
    return rows


def _repos(cfg: config.Config) -> list[RepoRow]:
    """``restic check --no-lock`` against configured repos. Bounded; never blocks."""
    cb = cfg.cli.cloud_backup
    if cb is None or not shutil.which("restic"):
        return []
    repos: list[tuple[str, str, str]] = []  # (label, path, keychain_password)
    try:
        from sanctum_cli import keychain

        password = keychain.read(
            account=cb.primary.keychain.account if cb.primary else "",
            service=cb.primary.keychain.service if cb.primary else "",
        )
    except LocalError as exc:
        return [
            RepoRow(
                repo="(restic)",
                status="UNKNOWN",
                detail=f"keychain unavailable: {exc.message[:80]}",
            )
        ]
    if cb.primary is not None:
        repos.append(("primary", cb.primary.repo, password))
    if cb.secondary is not None:
        repos.append(("secondary", cb.secondary.repo, password))

    rows: list[RepoRow] = []
    for _label, repo, pwd in repos:
        rows.append(_restic_check(repo, pwd))
    return rows


def _restic_check(repo: str, password: str) -> RepoRow:
    import os as _os

    env = dict(_os.environ)
    env["RESTIC_PASSWORD"] = password
    try:
        out = subprocess.run(
            ["restic", "-r", repo, "snapshots", "--json", "--latest", "1", "--no-lock"],
            capture_output=True,
            text=True,
            timeout=RESTIC_PROBE_TIMEOUT_S,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return RepoRow(repo=repo, status="UNKNOWN", detail="restic probe timed out")

    if out.returncode != 0:
        last = (out.stderr.strip().splitlines() or ["restic failed"])[-1]
        return RepoRow(repo=repo, status="FAILED", detail=last[:160])
    return RepoRow(repo=repo, status="OPERATIONAL", detail=None)


# ─── Helpers ────────────────────────────────────────────────────────


# REPORTING ranks with OPERATIONAL (0): a monitor's designed non-zero exit must
# NOT drag `overall` into degraded/failed — red is reserved for real breakage.
_ORDER: dict[Status, int] = {
    "OPERATIONAL": 0,
    "REPORTING": 0,
    "DEGRADED": 1,
    "UNKNOWN": 2,
    "FAILED": 3,
}


def _worse(a: Status, b: Status) -> Status:
    return a if _ORDER[a] >= _ORDER[b] else b


def _color(status: Status) -> str:
    return {
        "OPERATIONAL": "[green]OPERATIONAL[/]",
        "REPORTING": "[cyan]REPORTING[/]",
        "DEGRADED": "[yellow]DEGRADED[/]",
        "FAILED": "[red]FAILED[/]",
        "UNKNOWN": "[dim]UNKNOWN[/]",
    }[status]


# ─── Renderers ──────────────────────────────────────────────────────


def render_brief(report: Report) -> str:
    counts = {
        "OPERATIONAL": 0,
        "REPORTING": 0,
        "DEGRADED": 0,
        "FAILED": 0,
        "UNKNOWN": 0,
    }
    rows: Iterable[Status] = (
        *(a.status for a in report.agents),
        *(p.status for p in report.providers),
        *(r.status for r in report.repos),
    )
    for s in rows:
        counts[s] += 1
    return (
        f"sanctum doctor: {report.overall.lower()} · "
        f"{len(report.agents)} agents · "
        f"{len(report.providers)} providers · "
        f"{len(report.repos)} repos · "
        f"{counts['REPORTING']} reporting · "
        f"{counts['DEGRADED']} degraded · {counts['FAILED']} failed"
    )


def render_full(report: Report) -> None:
    if report.agents:
        t = Table(title="LaunchAgents (com.sanctum.*)", show_header=True, header_style="bold")
        t.add_column("label")
        t.add_column("pid", justify="right")
        t.add_column("last exit", justify="right")
        t.add_column("status", justify="right")
        for a in report.agents:
            t.add_row(a.label, a.pid, a.last_exit, _color(a.status))
        console.print(t)
        console.print()

    if report.providers:
        t = Table(title="Providers", show_header=True, header_style="bold")
        t.add_column("name")
        t.add_column("latency", justify="right")
        t.add_column("status", justify="right")
        t.add_column("detail")
        for p in report.providers:
            lat = f"{p.latency_ms} ms" if p.latency_ms is not None else "—"
            t.add_row(p.name, lat, _color(p.status), p.detail or "")
        console.print(t)
        console.print()

    if report.repos:
        t = Table(title="Backup repos", show_header=True, header_style="bold")
        t.add_column("repo")
        t.add_column("status", justify="right")
        t.add_column("detail")
        for r in report.repos:
            t.add_row(_abbrev(r.repo, 60), _color(r.status), r.detail or "")
        console.print(t)
        console.print()


def _abbrev(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ─── Aggregator ─────────────────────────────────────────────────────


def collect(cfg: config.Config) -> Report:
    return Report(agents=_agents(), providers=_providers(cfg), repos=_repos(cfg))


def doctor_command(
    full: Annotated[bool, typer.Option("--full", help="Always print full per-row detail.")] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON (full report regardless of --full).")
    ] = False,
    fix: bool = False,
) -> None:
    cfg = config.load()

    if fix:
        import json
        import os
        import shutil
        import time
        import urllib.request
        from pathlib import Path

        report_before = collect(cfg)
        _probes = _daemon_probes()
        for a in report_before.agents:
            if a.status in ("FAILED", "DEGRADED"):
                label = a.label
                # Never resurrect a service that is already answering its health
                # probe: loading a second copy fights the live one for its port
                # (AddrInUse) — the failure mode that crash-loops the duplicate.
                if label in _probes and _http_ok(_probes[label]):
                    continue
                plist_path = Path.home() / f"Library/LaunchAgents/{label}.plist"
                if not plist_path.exists():
                    target_dir = plist_path.parent
                    target_dir.mkdir(parents=True, exist_ok=True)
                    candidates = [
                        Path(f"/Users/bert/Library/LaunchAgents/{label}.plist"),
                        Path(f"/Users/bert/Projects/sanctum-runtime/launchagents/{label}.plist"),
                        Path(
                            f"/Users/bert/Projects/Claude_Code/sanctum/launchagents/{label}.plist"
                        ),
                    ]
                    for src in candidates:
                        if src.exists():
                            shutil.copy(src, plist_path)
                            break

                if plist_path.exists():
                    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
                    time.sleep(0.5)
                    subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)

                    url = os.environ.get("FORCE_FLOW_URL", "http://127.0.0.1:4077")
                    try:
                        req = urllib.request.Request(
                            f"{url}/notify",
                            data=json.dumps(
                                {
                                    "source": "doctor",
                                    "severity": "info",
                                    "title": f"LaunchAgent Recovery: {label}",
                                    "message": f"Automatically restarted drifted LaunchAgent {label}.",
                                }
                            ).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                        )
                        with urllib.request.urlopen(req, timeout=2):
                            pass
                    except Exception:
                        pass

    report = collect(cfg)

    if json_output:
        import json as _json

        payload = {
            "overall": report.overall,
            "agents": [_dataclass_dict(a) for a in report.agents],
            "providers": [_dataclass_dict(p) for p in report.providers],
            "repos": [_dataclass_dict(r) for r in report.repos],
        }
        print(_json.dumps(payload, indent=2))
        return

    if not full and report.overall == "OPERATIONAL":
        print(render_brief(report))
        return

    render_full(report)
    summary = render_brief(report)
    if report.overall != "OPERATIONAL":
        err.print(f"\n[bold]{summary}[/]")
    else:
        console.print(f"\n[bold]{summary}[/]")


def _dataclass_dict(obj: object) -> dict[str, object]:
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is None:
        return {}
    return {k: getattr(obj, k) for k in fields}
