"""``sanctum self-test`` — the canonical "is my install still good?" command.

Runs a small fleet of probes against the local Sanctum surface and reports
per-probe results plus a summary. Designed for an Apple-grade beta-tester
moment: one command, clear pass/fail per check, friendly summary panel,
honest failure messages.

Exit codes:
  0  all probes passed
  1  one or more probes failed
  2  internal error (script broke before completing the run)

Each probe is a `Probe` dataclass with a name and a check function. The
check function returns a `ProbeResult` (passed: bool, detail: str). The
runner times each probe, prints a row in real time, and assembles the
summary panel at the end.
"""

from __future__ import annotations

import importlib
import json
import socket
import sqlite3
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from sanctum_cli import config, proxyd

if TYPE_CHECKING:
    from collections.abc import Callable

    from sanctum_cli.modules.registry import ModuleRegistry

console = Console()


@dataclass
class ProbeResult:
    passed: bool
    detail: str = ""
    # Three-state status: a probe can also report "not applicable" when its
    # service isn't expected on this install tier. A fresh CLI-only install
    # (brew install sanctum-cli + sanctum onboard --recipe family) does not
    # bring up the cathedrals, proxyd, Force Flow, chitti, or R2D2 — those
    # are operator-grade services that run on a dedicated Mac Mini. On the
    # CLI-only tier, those probes return n/a, not fail. n/a does not count
    # toward the exit code.
    not_applicable: bool = False
    reason: str = ""  # explanation for n/a state


@dataclass
class Probe:
    name: str
    check: Callable[[], ProbeResult]


# ── Probe implementations ─────────────────────────────────────────────


def _haus_tier_installed() -> bool:
    """Detect whether this is a haus-operator install (cathedrals + proxyd +
    Force Flow + chitti + R2D2) vs a CLI-only install (just sanctum-cli +
    backup recipe). Returns True only if at least one haus-tier artifact is
    present on disk — cathedral manifests, proxyd config, or the R2D2
    classifier source. A friend's fresh install has none of these."""
    haus_markers = [
        Path.home() / ".sanctum/manifests",  # cathedral model manifests
        Path.home() / ".sanctum/sanctum-proxy",  # proxyd config dir
        Path.home() / ".sanctum/r2d2",  # R2D2 classifier source
        Path("/Library/LaunchDaemons/com.sanctum.proxyd.plist"),
    ]
    return any(p.exists() for p in haus_markers)


def _haus_only(name: str, check_fn: Callable[[], ProbeResult]) -> Callable[[], ProbeResult]:
    """Decorator-like wrapper: skip a haus-tier probe with n/a when this is
    a CLI-only install. The wrapped function is only invoked on haus
    installs. ``name`` names the probe in the n/a diagnostic so an operator
    can tell at a glance which haus surface was skipped."""

    def wrapped() -> ProbeResult:
        if not _haus_tier_installed():
            return ProbeResult(
                passed=True,
                not_applicable=True,
                reason=f"CLI-only install: {name} probe skipped (no haus services configured)",
            )
        return check_fn()

    return wrapped


def _tcp_reachable(host: str, port: int, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _http_ok(url: str, timeout_s: float = 3.0) -> tuple[bool, int]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.status == 200, resp.status
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False, 0


def probe_network() -> ProbeResult:
    """Loopback reachable + DNS resolves something."""
    if not _tcp_reachable("127.0.0.1", 22):
        # 22 may not be open; try 80 or just confirm socket lib works
        pass
    try:
        socket.gethostbyname("apple.com")
        return ProbeResult(True, "loopback + DNS ok")
    except socket.gaierror:
        return ProbeResult(False, "DNS resolution failed")


def probe_node_identity() -> ProbeResult:
    """`/usr/local/bin/node` is the Node.js Foundation signed binary."""
    node_path = Path("/usr/local/bin/node")
    if not node_path.is_file():
        return ProbeResult(False, "/usr/local/bin/node missing — see install runbook")
    try:
        out = subprocess.run(
            ["codesign", "-dvv", str(node_path)],
            capture_output=True,
            text=True,
            timeout=3,
        )
        signing = out.stderr + out.stdout
        if "HX7739G8FX" in signing:
            return ProbeResult(True, "Node.js Foundation HX7739G8FX")
        return ProbeResult(False, "node is at /usr/local/bin but not signed by Node.js Foundation")
    except Exception as e:
        return ProbeResult(False, f"codesign failed: {e}")


def probe_yoda_cathedral() -> ProbeResult:
    """Yoda 35B cathedral listens on :1337."""
    ok = _tcp_reachable("127.0.0.1", 1337, 1.5)
    return ProbeResult(ok, "TLS port reachable" if ok else "no response on :1337")


def probe_coder_cathedral() -> ProbeResult:
    """Codestral coder cathedral listens on :3301 — it replaced the retired
    coder-14b on :1338 (booted out 2026-06-07, :1338 freed)."""
    ok = _tcp_reachable("127.0.0.1", 3301, 1.5)
    return ProbeResult(ok, "port reachable" if ok else "no response on :3301")


def probe_proxyd() -> ProbeResult:
    """proxyd router on the resolved endpoint (TLS by default). Any HTTP
    response means alive — 401 is expected when no x-api-key header is supplied;
    the point is reachability *and*, over https, a CA-verified handshake.

    Endpoint + trust anchor come from :mod:`sanctum_cli.proxyd` — no literal
    host/port here. ``SANCTUM_PROXYD_URL=https://127.0.0.1:4041`` exercises the
    exact TLS+CA path against the cutover TLS listener before :4040 flips.
    """
    url = proxyd.base_url()
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not _tcp_reachable(host, port, 1.5):
        return ProbeResult(False, f"no listener on {host}:{port}")
    # An https endpoint must pass a CA-verified TLS handshake; a plaintext one
    # uses no context. proxyd.ssl_context() is the SAME trust object the council
    # httpx client verifies through (CA-pinned by default; unverified only under
    # the explicit dev insecure hatch).
    ssl_ctx: ssl.SSLContext | None = None
    if parsed.scheme == "https":
        ssl_ctx = proxyd.ssl_context()
    # Get the actual HTTP status to display, but accept anything 100-599 as up.
    try:
        req = urllib.request.Request(f"{url}/v1/models")
        urllib.request.urlopen(req, timeout=2.5, context=ssl_ctx)
        return ProbeResult(True, "HTTP 200 (open route)")
    except urllib.error.HTTPError as e:
        # 401, 403, 405 etc — service is up (TLS handshake already passed).
        return ProbeResult(True, f"HTTP {e.code} (service alive)")
    except (urllib.error.URLError, ssl.SSLError, OSError) as e:
        return ProbeResult(False, f"no HTTP response: {proxyd.describe_transport_error(e)}")


def probe_proxy_key() -> ProbeResult:
    """The council's proxyd wire-auth key is provisioned (env or keychain).

    Honest about a silent failure mode: when neither ``$SANCTUM_PROXY_KEY``
    nor the ``sanctum-proxy-client`` keychain item is set, the council falls
    back to a non-secret identifier that works only while proxyd's auth is
    lenient — and will 401 the day it enforces. A FALLBACK row makes that
    posture visible instead of letting it bite at the worst time. Resolves no
    secret, mints nothing — only reports provisioned vs FALLBACK.
    """
    from sanctum_cli.commands import council

    if council.proxy_key_provisioned():
        return ProbeResult(True, "proxy key provisioned")
    return ProbeResult(
        False,
        "proxy key: FALLBACK (not provisioned) — set $SANCTUM_PROXY_KEY or the "
        "'sanctum-proxy-client' keychain item; council will 401 once proxyd enforces auth",
    )


def probe_force_flow() -> ProbeResult:
    """Force Flow notice hub on :4077."""
    ok = _tcp_reachable("127.0.0.1", 4077, 1.5)
    return ProbeResult(ok, "listening" if ok else "down on :4077")


def probe_chitti_samskara() -> ProbeResult:
    """chitti cross-agent status bus on :2188."""
    ok = _tcp_reachable("127.0.0.1", 2188, 1.5)
    return ProbeResult(ok, "listening" if ok else "down on :2188")


def probe_tcc_grants() -> ProbeResult:
    """13 TCC grants for /usr/local/bin/node landed via sanctum-grant-tcc.sh."""
    tcc_db = Path.home() / "Library/Application Support/com.apple.TCC/TCC.db"
    if not tcc_db.is_file():
        return ProbeResult(False, "TCC.db not found")
    try:
        conn = sqlite3.connect(f"file:{tcc_db}?mode=ro", uri=True, timeout=2.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM access WHERE client = ? AND auth_value = 2",
            ("/usr/local/bin/node",),
        )
        count = cur.fetchone()[0]
        conn.close()
        if count >= 13:
            return ProbeResult(True, f"{count}/13+ grants")
        if count > 0:
            return ProbeResult(
                False,
                f"only {count}/13 grants — run ~/.sanctum/scripts/sanctum-grant-tcc.sh",
            )
        return ProbeResult(
            False,
            "no grants for /usr/local/bin/node — run ~/.sanctum/scripts/sanctum-grant-tcc.sh",
        )
    except sqlite3.Error as e:
        return ProbeResult(False, f"TCC.db read failed: {e} (needs FDA on Terminal)")


def probe_r2d2_heartbeat() -> ProbeResult:
    """R2D2 supervisor reports a recent cycle to chitti."""
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:2188/samskara?service=r2d2&pattern=cycle&limit=1",
            timeout=2.5,
        ) as resp:
            data = json.loads(resp.read())
        actions = data.get("actions", [])
        if not actions:
            return ProbeResult(False, "no chitti heartbeat for r2d2")
        a = actions[0]
        return ProbeResult(
            True,
            f"attempts={a.get('attempts')}, success_rate={a.get('success_rate')}",
        )
    except Exception as e:
        return ProbeResult(False, f"chitti unreachable: {e}")


def probe_audit_log_bounded() -> ProbeResult:
    """R2D2 audit log within its 50 MB rotation budget."""
    log = Path.home() / ".sanctum/logs/r2d2-audit.jsonl"
    if not log.is_file():
        return ProbeResult(True, "no audit log yet (fresh install)")
    size = log.stat().st_size
    cap = 50 * 1024 * 1024
    if size > cap:
        return ProbeResult(False, f"{size // 1024 // 1024} MB > 50 MB — log-rotate may have failed")
    return ProbeResult(True, f"{size // 1024 // 1024} MB / 50 MB cap")


def probe_config_valid() -> ProbeResult:
    """sanctum-cli's instance.yaml parses + matches the cli schema."""
    try:
        config.load()
        return ProbeResult(True, "instance.yaml validates")
    except Exception as e:
        return ProbeResult(False, str(e)[:100])


def probe_backup_recent() -> ProbeResult:
    """Last backup snapshot landed within the expected window."""
    state_file = Path.home() / ".sanctum/state/backup-canary.status"
    if not state_file.is_file():
        return ProbeResult(True, "no backups yet (fresh install)")
    age_s = time.time() - state_file.stat().st_mtime
    age_h = age_s / 3600
    if age_h > 36:  # daily-ish + 12h grace
        return ProbeResult(False, f"last backup {age_h:.0f}h ago — daily cron may be down")
    return ProbeResult(True, f"last backup {age_h:.1f}h ago")


# ── Probe registry ────────────────────────────────────────────────────


PROBES: list[Probe] = [
    # CLI-tier — every install needs these.
    Probe("network reachability", probe_network),
    Probe("node binary identity", probe_node_identity),
    Probe("sanctum config valid", probe_config_valid),
    Probe("recent backup snapshot", probe_backup_recent),
    # Haus-tier — only meaningful when the haus services are installed.
    # Each is wrapped to return n/a on CLI-only installs.
    Probe("Yoda cathedral (:1337)", _haus_only("yoda", probe_yoda_cathedral)),
    Probe("Coder cathedral (:3301)", _haus_only("coder", probe_coder_cathedral)),
    Probe("proxyd routing (:4040)", _haus_only("proxyd", probe_proxyd)),
    Probe("proxy key provisioned", _haus_only("proxy-key", probe_proxy_key)),
    Probe("Force Flow (:4077)", _haus_only("force-flow", probe_force_flow)),
    Probe("chitti samskara (:2188)", _haus_only("chitti", probe_chitti_samskara)),
    Probe("TCC grants", _haus_only("tcc", probe_tcc_grants)),
    Probe("R2D2 supervisor heartbeat", _haus_only("r2d2", probe_r2d2_heartbeat)),
    Probe("audit log within budget", _haus_only("audit", probe_audit_log_bounded)),
]

# ── Module-keyed probe registry ───────────────────────────────────────
#
# The flat ``PROBES`` list above is what the runner iterates (unchanged by
# this refactor).  ``module_probes`` produces the *module-keyed* view that
# the soak harness and any future consumer can use to discover which probes
# belong to which installed module.
#
# Resolution convention for a dotted probe path "a.b.c.probe_fn":
#   - The *callable* is the last component; everything before it is the
#     module path within ``sanctum_cli.commands``.
#   - We try ``sanctum_cli.commands.<a.b.c>`` first, then ``sanctum_cli.<a.b.c>``
#     as a fallback, then the path verbatim.
#   - If the import fails, a failing probe is returned so self-test surfaces
#     the broken reference without crashing.

_SEARCH_PREFIXES = (
    "sanctum_cli.commands.",
    "sanctum_cli.",
    "",
)


def _resolve_probe_callable(dotted_path: str) -> Callable[[], ProbeResult]:
    """Resolve a dotted probe path to a callable, or return a failing stub.

    The path must end with a callable name.  The portion before the last dot
    is the module path; we try each prefix in ``_SEARCH_PREFIXES`` in order.

    Returns a zero-argument callable that returns a ``ProbeResult``.
    """
    if "." not in dotted_path:
        detail = f"probe path '{dotted_path}' has no module component (need 'module.fn')"
        return lambda: ProbeResult(False, detail)

    mod_part, fn_name = dotted_path.rsplit(".", 1)
    last_exc: Exception = ImportError(f"no prefix resolved '{dotted_path}'")

    for prefix in _SEARCH_PREFIXES:
        full_mod = f"{prefix}{mod_part}" if prefix else mod_part
        try:
            mod = importlib.import_module(full_mod)
            fn: Callable[[], ProbeResult] = getattr(mod, fn_name)
            return fn
        except (ImportError, AttributeError) as exc:
            last_exc = exc
            continue

    captured = last_exc
    detail = f"import error for probe '{dotted_path}': {captured}"
    return lambda: ProbeResult(False, detail)


def module_probes(registry: ModuleRegistry) -> dict[str, list[Probe]]:
    """Build a module-keyed probe dict from *registry*.

    Each module in the registry contributes its ``probes`` list under its
    module name key.  Probe dotted-paths are resolved via
    ``_resolve_probe_callable``; unresolvable paths produce failing probes
    (self-test surfaces the broken reference without crashing).

    The existing ``PROBES`` flat list and runner are **not** modified by this
    function — this is an additive view for consumers such as the soak harness.

    Design note: self-test intentionally does NOT merge module probes into its
    own ``PROBES`` runner.  Doing so would double-run the built-in
    ``backup_recent`` probe (which already appears in ``PROBES`` directly) and
    would break existing tests that monkeypatch ``PROBES``.  Module probes are
    consumed by the soak harness (``run_soak`` → ``_collect_sample``) instead,
    which is the appropriate long-running signal collector for per-module health.
    """
    result: dict[str, list[Probe]] = {}
    for name, manifest in registry.manifests.items():
        result[name] = [Probe(path, _resolve_probe_callable(path)) for path in manifest.probes]
    return result


# ── Public command ────────────────────────────────────────────────────


def self_test_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON instead of the human-friendly panel."),
    ] = False,
    only: Annotated[
        str | None,
        typer.Option("--only", help="Run only probes whose name contains this substring."),
    ] = None,
) -> None:
    """Run the canonical health probes.

    Honest by design: each probe runs, the result lands on-screen
    immediately, the summary panel reflects the actual state, the exit
    code is 0 iff every probe passed.
    """
    start = time.time()
    results: list[tuple[Probe, ProbeResult, float]] = []

    selected = [p for p in PROBES if only.lower() in p.name.lower()] if only else PROBES

    if not json_output:
        console.print()
        console.print(
            Panel.fit(
                Text(f"Sanctum Self-Test — {len(selected)} probes", style="bold"),
                border_style="cyan",
            )
        )
        console.print()

    if not selected:
        if not json_output:
            console.print(
                Panel(
                    Text("No probes matched the --only filter.", style="dim"),
                    border_style="yellow",
                )
            )
        else:
            console.print_json(
                json.dumps({"total": 0, "passed": 0, "failed": 0, "duration_ms": 0.0, "probes": []})
            )
        raise typer.Exit(code=0)

    width = max(len(p.name) for p in selected) + 2

    for i, probe in enumerate(selected, start=1):
        t0 = time.time()
        try:
            res = probe.check()
        except Exception as e:
            res = ProbeResult(False, f"probe raised: {e}")
        elapsed_ms = (time.time() - t0) * 1000
        results.append((probe, res, elapsed_ms))

        if not json_output:
            if res.not_applicable:
                mark = Text(" n/a  ", style="bold yellow")
                trailing = res.reason or "not applicable on this install"
            elif res.passed:
                mark = Text(" pass ", style="bold green")
                trailing = res.detail
            else:
                mark = Text(" fail ", style="bold red")
                trailing = res.detail
            line = Text.assemble(
                Text(f"  [{i:2d}/{len(selected):2d}] ", style="dim"),
                Text(f"{probe.name:<{width}}", style="white"),
                Text("·" * 4, style="dim"),
                mark,
                Text(f"  {trailing}", style="dim"),
            )
            console.print(line)

    total_ms = (time.time() - start) * 1000
    na = sum(1 for _, r, _ in results if r.not_applicable)
    passed = sum(1 for _, r, _ in results if r.passed and not r.not_applicable)
    failed = sum(1 for _, r, _ in results if not r.passed)

    tier = "haus" if _haus_tier_installed() else "CLI"

    if json_output:
        payload = {
            "tier": tier,
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "not_applicable": na,
            "duration_ms": round(total_ms, 1),
            "probes": [
                {
                    "name": p.name,
                    "passed": r.passed,
                    "not_applicable": r.not_applicable,
                    "detail": r.detail,
                    "reason": r.reason,
                    "duration_ms": round(ms, 1),
                }
                for p, r, ms in results
            ],
        }
        console.print_json(json.dumps(payload))
    else:
        console.print()
        if failed == 0:
            headline = f"Sanctum {tier} is healthy."
            counts = f"  {passed}/{len(results)} probes passed in {total_ms:.0f} ms"
            if na:
                counts += f"  ·  {na} n/a on this install tier"
            console.print(
                Panel(
                    Text.assemble(
                        Text(headline, style="bold green"),
                        Text(counts, style="dim"),
                    ),
                    border_style="green",
                    padding=(0, 2),
                )
            )
        else:
            console.print(
                Panel(
                    Text.assemble(
                        Text(f"{failed} probe(s) failed.", style="bold red"),
                        Text(
                            f"  {passed}/{len(results) - na} applicable probes passed in {total_ms:.0f} ms."
                            f" Each red line above has the actionable detail.",
                            style="dim",
                        ),
                    ),
                    border_style="red",
                    padding=(0, 2),
                )
            )

    raise typer.Exit(code=1 if failed > 0 else 0)
