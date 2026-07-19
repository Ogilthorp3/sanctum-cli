"""``sanctum upgrade`` — one brain for the whole sanctum toolchain.

``sanctum update`` refreshes the CLI itself; this command looks after
everything the CLI *rides on*: the brew formulae, npm globals, and pip
venvs that make sanctum smarter (agent runtimes, local inference),
safer (mesh, secrets, backups), and faster (runtimes, tooling).

Contract:

* ``check`` (default) is READ-ONLY: it inventories the registry against
  this machine, resolves the latest *stable* version per manager (brew
  stable bottles, npm ``latest`` dist-tag — never beta/alpha, pip final
  releases), and prints an old→new table. Exit 1 when upgrades exist so
  sentinels/cron can key off it, 0 when the toolchain is current.
* ``--apply`` upgrades only what the plan showed, one tool at a time,
  re-probes the installed version afterwards (honest-verify: the number
  in the "now" column is re-read from the machine, never assumed), runs
  each tool's post-check, and finishes with the ``sanctum self-test``
  gate. Daemons that ride on an upgraded tool get an explicit restart
  hint — an upgrade that silently needs a kickstart is a 3am page.
* ``brew pin`` is respected: pinned formulae report HOLD and are never
  touched. Tools not installed on this machine are skipped, so the same
  registry serves the hub, the satellites, and the console.

Every impure seam (subprocess) goes through an injectable runner so the
contract is testable without touching brew/npm/pip.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
err = Console(stderr=True)

PROBE_TIMEOUT_S = 20
UPGRADE_TIMEOUT_S = 900

Manager = Literal["brew", "npm", "pip-venv"]
State = Literal["ok", "upgrade", "hold", "absent", "unknown"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool sanctum depends on, and how to keep it current."""

    name: str
    manager: Manager
    package: str
    roles: tuple[str, ...]
    why: str
    venv: str | None = None
    npm_aliases: tuple[str, ...] = ()
    post_cmd: tuple[str, ...] | None = None
    restart_hint: str | None = None


# The curated registry: what sanctum actually rides on, machine-agnostic.
# Discovery skips anything not installed, so the hub / satellites / console
# all share this one list.
REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        "sanctum-cli",
        "brew",
        "sanctum-cli",
        ("smarter",),
        "the product itself",
        post_cmd=("sanctum", "--help"),
    ),
    ToolSpec(
        "tailscale",
        "brew",
        "tailscale",
        ("safer",),
        "the mesh every node lives on",
        post_cmd=("tailscale", "version"),
        restart_hint="brew services restart tailscale (mesh daemon rides on it)",
    ),
    ToolSpec(
        "node",
        "brew",
        "node",
        ("safer", "faster"),
        "runtime under openclaw + agents",
        post_cmd=("node", "--version"),
        restart_hint="restart the openclaw gateway (it runs on this node binary)",
    ),
    ToolSpec(
        "colima",
        "brew",
        "colima",
        ("safer",),
        "container host under Home Assistant",
        post_cmd=("colima", "version"),
        restart_hint="colima restart (HA container rides on it)",
    ),
    ToolSpec("lima", "brew", "lima", ("safer",), "VM engine under colima"),
    ToolSpec("docker", "brew", "docker", ("safer",), "container client"),
    ToolSpec(
        "restic",
        "brew",
        "restic",
        ("safer",),
        "backup engine",
        post_cmd=("restic", "version"),
    ),
    ToolSpec("sops", "brew", "sops", ("safer",), "secrets at rest (SOPS-first doctrine)"),
    ToolSpec("age", "brew", "age", ("safer",), "encryption under sops"),
    ToolSpec("gh", "brew", "gh", ("smarter",), "repo + PR operations"),
    ToolSpec(
        "python@3.12",
        "brew",
        "python@3.12",
        ("faster",),
        "interpreter under local brains",
        restart_hint="venvs pinned to this interpreter may need a rebuild on minor bumps",
    ),
    ToolSpec("uv", "brew", "uv", ("faster",), "python packaging/tooling"),
    ToolSpec("gemini-cli", "brew", "gemini-cli", ("smarter",), "google lane"),
    ToolSpec(
        "cloudflared",
        "brew",
        "cloudflared",
        ("safer",),
        "tunnel under the off-site deadman eye",
        restart_hint="restart the cloudflared tunnel service",
    ),
    ToolSpec(
        "openclaw",
        "npm",
        "openclaw",
        ("smarter",),
        "agent runtime (council + satellites)",
        npm_aliases=("denchclaw",),
        post_cmd=("openclaw", "--version"),
        restart_hint="restart the openclaw gateway to pick up the new runtime",
    ),
    ToolSpec(
        "agent-browser",
        "npm",
        "agent-browser",
        ("smarter",),
        "browser hands for agents",
    ),
    ToolSpec(
        "claude-max-api-proxy",
        "npm",
        "claude-max-api-proxy",
        ("smarter",),
        "claude bridge (:3456)",
        restart_hint="restart the claude-max bridge service",
    ),
    ToolSpec(
        "grok",
        "npm",
        "@xai-official/grok",
        ("smarter",),
        "grok lane (Mundi)",
    ),
    ToolSpec(
        "mlx-lm",
        "pip-venv",
        "mlx-lm",
        ("smarter", "faster"),
        "local inference server (satellite brains)",
        venv="~/.sanctum/mlx-venv",
        restart_hint="launchctl kickstart the mlx server daemon that serves this venv",
    ),
)


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., RunResult]


def _run(cmd: list[str], timeout: int = PROBE_TIMEOUT_S) -> RunResult:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return RunResult(127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"{cmd[0]}: timed out after {timeout}s")
    return RunResult(r.returncode, r.stdout, r.stderr)


@dataclass(frozen=True, slots=True)
class PlanRow:
    spec: ToolSpec
    state: State
    installed: str
    latest: str
    note: str = ""


# ── inventory per manager ────────────────────────────────────────────────


def brew_state(run: Runner) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Return (installed, latest-for-outdated, pinned) for brew formulae.

    ``latest`` only has entries for outdated formulae — an installed
    formula missing from it is already current.
    """
    installed: dict[str, str] = {}
    r = run(["brew", "list", "--versions"])
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            installed[parts[0]] = parts[1]

    latest: dict[str, str] = {}
    r = run(["brew", "outdated", "--json=v2"])
    if r.returncode == 0 and r.stdout.strip():
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            data = {}
        for f in data.get("formulae", []):
            name = f.get("name", "")
            cur = f.get("current_version", "")
            if name and cur:
                latest[name] = cur
                vs = f.get("installed_versions") or []
                if vs:
                    installed.setdefault(name, vs[-1])

    pinned = {
        ln.strip() for ln in run(["brew", "list", "--pinned"]).stdout.splitlines() if ln.strip()
    }
    return installed, latest, pinned


def npm_installed(run: Runner) -> dict[str, str]:
    """Global npm packages as {name-or-alias: version}."""
    r = run(["npm", "ls", "-g", "--depth=0", "--json"])
    if not r.stdout.strip():
        return {}
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}
    deps = data.get("dependencies") or {}
    return {name: info.get("version", "") for name, info in deps.items() if isinstance(info, dict)}


def npm_latest(run: Runner, package: str) -> str:
    """Latest STABLE (dist-tag ``latest``) — betas never qualify."""
    r = run(["npm", "view", package, "dist-tags.latest"])
    return r.stdout.strip() if r.returncode == 0 else ""


def venv_pip(venv: str) -> list[str]:
    """Invoke pip as ``<venv>/bin/python -m pip`` — the pip console script's
    shebang bakes in the venv's creation path, so a renamed/relocated venv
    has a working python but a broken pip binary (found live on the chalet)."""
    return [str(Path(venv).expanduser() / "bin" / "python"), "-m", "pip"]


def pip_venv_state(run: Runner, venv: str, package: str) -> tuple[str, str]:
    """(installed, latest-stable) for a package inside a venv; '' if absent."""
    if not (Path(venv).expanduser() / "bin" / "python").exists():
        return "", ""
    pip = venv_pip(venv)
    installed = ""
    r = run([*pip, "show", package])
    for line in r.stdout.splitlines():
        if line.lower().startswith("version:"):
            installed = line.split(":", 1)[1].strip()
            break
    if not installed:
        return "", ""
    latest = installed
    r = run([*pip, "index", "versions", package])
    # First line: "package (X.Y.Z)" — pip already excludes pre-releases.
    if r.returncode == 0 and "(" in r.stdout:
        latest = r.stdout.split("(", 1)[1].split(")", 1)[0].strip()
    return installed, latest


# ── plan ─────────────────────────────────────────────────────────────────


def build_plan(run: Runner, only: set[str] | None = None) -> list[PlanRow]:
    specs = [s for s in REGISTRY if only is None or s.name in only]
    rows: list[PlanRow] = []

    brew_specs = [s for s in specs if s.manager == "brew"]
    if brew_specs:
        installed, latest, pinned = brew_state(run)
        for s in brew_specs:
            cur = installed.get(s.package, "")
            if not cur:
                rows.append(PlanRow(s, "absent", "", ""))
            elif s.package in pinned:
                rows.append(PlanRow(s, "hold", cur, latest.get(s.package, cur), "brew pin"))
            elif s.package in latest:
                rows.append(PlanRow(s, "upgrade", cur, latest[s.package]))
            else:
                rows.append(PlanRow(s, "ok", cur, cur))

    npm_specs = [s for s in specs if s.manager == "npm"]
    if npm_specs:
        inst = npm_installed(run)

        def resolve(s: ToolSpec) -> PlanRow:
            names = (s.package, *s.npm_aliases)
            found = next((n for n in names if n in inst), None)
            if found is None:
                return PlanRow(s, "absent", "", "")
            cur = inst[found]
            new = npm_latest(run, s.package)
            note = f"installed as {found}" if found != s.package else ""
            if not new:
                return PlanRow(s, "unknown", cur, "", "npm registry unreachable")
            if new != cur:
                return PlanRow(s, "upgrade", cur, new, note)
            return PlanRow(s, "ok", cur, cur, note)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(resolve, s): s for s in npm_specs}
            npm_rows = {futures[f].name: f.result() for f in as_completed(futures)}
        rows.extend(npm_rows[s.name] for s in npm_specs)

    for s in specs:
        if s.manager != "pip-venv":
            continue
        assert s.venv is not None
        cur, new = pip_venv_state(run, s.venv, s.package)
        if not cur:
            rows.append(PlanRow(s, "absent", "", ""))
        elif new != cur:
            rows.append(PlanRow(s, "upgrade", cur, new))
        else:
            rows.append(PlanRow(s, "ok", cur, cur))

    return rows


# ── apply ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ApplyResult:
    row: PlanRow
    ok: bool
    now: str
    detail: str = ""


def upgrade_command_for(row: PlanRow) -> list[str]:
    s = row.spec
    if s.manager == "brew":
        return ["brew", "upgrade", s.package]
    if s.manager == "npm":
        alias = (
            row.note.removeprefix("installed as ") if row.note.startswith("installed as ") else None
        )
        target = f"{alias}@npm:{s.package}@{row.latest}" if alias else f"{s.package}@{row.latest}"
        return ["npm", "install", "-g", target]
    assert s.venv is not None
    return [*venv_pip(s.venv), "install", "--upgrade", f"{s.package}=={row.latest}"]


def reprobe_version(run: Runner, spec: ToolSpec) -> str:
    """Re-read the installed version after an upgrade — never assume."""
    if spec.manager == "brew":
        r = run(["brew", "list", "--versions", spec.package])
        parts = r.stdout.split()
        return parts[1] if len(parts) >= 2 else ""
    if spec.manager == "npm":
        inst = npm_installed(run)
        for name in (spec.package, *spec.npm_aliases):
            if name in inst:
                return inst[name]
        return ""
    assert spec.venv is not None
    cur, _ = pip_venv_state(run, spec.venv, spec.package)
    return cur


def apply_row(run: Runner, row: PlanRow) -> ApplyResult:
    r = run(upgrade_command_for(row), UPGRADE_TIMEOUT_S)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip()[-200:]
        return ApplyResult(row, False, row.installed, detail)
    now = reprobe_version(run, row.spec)
    if now == row.installed and now != row.latest:
        return ApplyResult(row, False, now, "version unchanged after upgrade")
    if row.spec.post_cmd is not None:
        pr = run(list(row.spec.post_cmd))
        if pr.returncode != 0:
            return ApplyResult(row, False, now, f"post-check failed: {' '.join(row.spec.post_cmd)}")
    return ApplyResult(row, True, now)


# ── rendering ────────────────────────────────────────────────────────────

_STATE_STYLE = {
    "ok": "[green]current[/]",
    "upgrade": "[yellow]upgrade[/]",
    "hold": "[cyan]HOLD[/]",
    "unknown": "[red]unknown[/]",
}


def _plan_table(rows: list[PlanRow]) -> Table:
    t = Table(box=None, pad_edge=False)
    t.add_column("tool", style="bold")
    t.add_column("via")
    t.add_column("makes sanctum")
    t.add_column("installed")
    t.add_column("latest stable")
    t.add_column("state")
    for row in rows:
        if row.state == "absent":
            continue
        arrow = f"[yellow]{row.latest}[/]" if row.state == "upgrade" else row.latest
        t.add_row(
            row.spec.name,
            row.spec.manager,
            "+".join(row.spec.roles),
            row.installed,
            arrow,
            _STATE_STYLE[row.state],
        )
    return t


def _rows_as_json(rows: list[PlanRow]) -> str:
    return json.dumps(
        [
            {
                "tool": r.spec.name,
                "manager": r.spec.manager,
                "roles": list(r.spec.roles),
                "installed": r.installed,
                "latest": r.latest,
                "state": r.state,
                "note": r.note,
            }
            for r in rows
        ],
        indent=2,
    )


# ── command ──────────────────────────────────────────────────────────────


def upgrade_command(
    apply: Annotated[
        bool, typer.Option("--apply", help="Perform the upgrades (default: report only).")
    ] = False,
    only: Annotated[
        str | None, typer.Option("--only", help="Comma-separated tool names to consider.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable plan output.")
    ] = False,
    skip_self_test: Annotated[
        bool,
        typer.Option("--skip-self-test", help="Skip the self-test gate after --apply."),
    ] = False,
    run: Runner = _run,
) -> None:
    """Inventory the toolchain, resolve latest stable, upgrade with proof."""
    only_set = {s.strip() for s in only.split(",") if s.strip()} if only else None
    if only_set is not None:
        known = {s.name for s in REGISTRY}
        unknown = only_set - known
        if unknown:
            err.print(f"[red]unknown tool(s):[/] {', '.join(sorted(unknown))}")
            err.print(f"[dim]registry: {', '.join(sorted(known))}[/]")
            raise typer.Exit(code=2)

    rows = build_plan(run, only_set)
    upgrades = [r for r in rows if r.state == "upgrade"]

    if json_output and not apply:
        console.print(_rows_as_json(rows))
        raise typer.Exit(code=1 if upgrades else 0)

    console.print()
    console.print(Panel.fit("[bold]sanctum upgrade[/] — toolchain currency", border_style="cyan"))
    console.print()
    console.print(_plan_table(rows))
    console.print()

    non_brew = [r for r in rows if r.state != "absent" and r.spec.manager != "brew"]
    if non_brew:
        console.print(
            f"[dim]{len(non_brew)} tool(s) live outside brew (npm/pip) — "
            f"candidates for brew-ification.[/]"
        )

    if not upgrades:
        console.print("[green]Toolchain is current.[/] Nothing to do.")
        raise typer.Exit(code=0)

    if not apply:
        names = ", ".join(r.spec.name for r in upgrades)
        console.print(f"[yellow]{len(upgrades)} upgrade(s) available:[/] {names}")
        console.print("[dim]Run `sanctum upgrade --apply` to take them.[/]")
        raise typer.Exit(code=1)

    console.print(f"[bold]Applying {len(upgrades)} upgrade(s)…[/]")
    results: list[ApplyResult] = []
    for row in upgrades:
        console.print(f"  {row.spec.name}: {row.installed} → {row.latest} …", end=" ")
        res = apply_row(run, row)
        results.append(res)
        console.print("[green]ok[/]" if res.ok else f"[red]FAILED[/] {res.detail}")

    console.print()
    t = Table(box=None, pad_edge=False)
    t.add_column("tool", style="bold")
    t.add_column("was")
    t.add_column("now")
    t.add_column("result")
    for res in results:
        t.add_row(
            res.row.spec.name,
            res.row.installed,
            res.now,
            "[green]ok[/]" if res.ok else f"[red]FAILED[/] {res.detail}",
        )
    console.print(t)

    hints = [res.row.spec.restart_hint for res in results if res.ok and res.row.spec.restart_hint]
    if hints:
        console.print()
        console.print("[bold]Restart hints (daemons ride on what just changed):[/]")
        for h in hints:
            console.print(f"  • {h}")

    failed = [res for res in results if not res.ok]

    if skip_self_test:
        console.print()
        console.print("[dim]--skip-self-test: run `sanctum self-test` manually.[/]")
        raise typer.Exit(code=1 if failed else 0)

    console.print()
    console.print("[bold]self-test gate[/]")
    from sanctum_cli.commands import self_test as st

    gate_ok = True
    try:
        st.self_test_command(json_output=False, only=None)
    except typer.Exit as exc:
        gate_ok = exc.exit_code in (0, None)

    raise typer.Exit(code=0 if gate_ok and not failed else 1)
