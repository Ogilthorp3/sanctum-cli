# Ship Bar + Module Spine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a module-manifest contract + `sanctum doctor --ship <module>` readiness gate + soak harness to sanctum-cli, additive over the v0.9 spine, with `backup` as the reference module that proves the loop.

**Architecture:** A new `sanctum_cli/modules/` package holds the pydantic manifest models and a registry that discovers built-in + user manifests. Pure gate functions in `ship_gates.py` score a module against the bar; `commands/ship.py` renders them (reusing the `self_test` runner shape). `commands/module.py` adds `module list/status/install/uninstall/demo`. A `soak/` package records 7-day unattended results that the soak gate reads. All additive — no rewrite of existing code.

**Tech Stack:** Python 3.11+, Typer, pydantic v2, Rich, PyYAML (already a dep via recipes), pytest, ruff + mypy strict, uv.

**Spec:** `docs/specs/2026-06-01-ship-bar-spine-design.md`

**Conventions for executors:**
- Follow existing patterns. Read the cited file before modifying. Key references: `sanctum_cli/commands/self_test.py` (ProbeResult/Probe/runner), `sanctum_cli/config.py` (CliConfig + pydantic style), `sanctum_cli/keychain.py` (read/exists), `sanctum_cli/recipes.py` (BUILTINS + resolve override pattern), `sanctum_cli/commands/uninstall.py` (bootout/rename/revoke).
- After every task: `uv run ruff check . && uv run mypy sanctum_cli && uv run pytest -q` must pass before committing.
- Commit messages use the repo's conventional style (`feat(modules): ...`), and end with the Co-Authored-By trailer for `Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

```
sanctum_cli/
  modules/
    __init__.py            # exports load_manifest, ModuleRegistry, model classes
    manifest.py            # pydantic models + load_manifest(path) + ManifestError
    registry.py            # ModuleRegistry: discover built-ins + user manifests, resolve deps
    builtins/
      backup.module.yaml   # reference module manifest (the CLI's own backup job)
  ship_gates.py            # pure gate functions → GateResult; GateStatus enum
  soak/
    __init__.py            # exports SoakResult, classify_soak, run_soak
    harness.py             # SoakResult schema, classify_soak(), run_soak() loop
  commands/
    module.py              # sanctum module list/status/install/uninstall/demo
    ship.py                # sanctum doctor --ship <module> (gate evaluator + render)
  config.py                # MODIFY: add CliConfig.modules: dict[str, ModuleConfig]
  commands/self_test.py    # MODIFY: PROBES flat list -> module-keyed registry + merge
  cli.py                   # MODIFY: register `module` sub-app, `--ship` on doctor, `soak`
tests/
  test_module_manifest.py
  test_module_registry.py
  test_backup_manifest.py
  test_ship_gates.py
  test_doctor_ship.py
  test_module_commands.py
  test_soak_harness.py
  test_probes_module_keyed.py
```

---

## Phase 1 — Module Manifest models + loader

### Task 1: Manifest pydantic models

**Files:**
- Create: `sanctum_cli/modules/__init__.py`
- Create: `sanctum_cli/modules/manifest.py`
- Test: `tests/test_module_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_module_manifest.py
import pytest
from sanctum_cli.modules.manifest import ModuleManifest, ManifestError, load_manifest

VALID = {
    "module": "backup",
    "version": "1.0.0",
    "description": "Encrypted restic backup to the operator's own cloud repo",
    "depends_on": [],
    "services": [],
    "secrets": [
        {"account": "sanctum", "service": "r2-access-key-id", "required": True, "generate": "none"}
    ],
    "probes": ["backup.backup_recent"],
    "alerts": {"sink": "chitti", "pager_conditions": []},
    "uninstall": {"bootout_labels": [], "revoke_secrets": ["r2-access-key-id"],
                  "remove_paths": [], "rename_suffix": ".uninstalled-{date}"},
    "docs": "https://example.invalid/backup",
    "demo": "sanctum backup snapshots",
    "soak": {"min_days": 7, "result_path": "~/.sanctum/soak/backup.json"},
}

def test_valid_manifest_parses():
    m = ModuleManifest.model_validate(VALID)
    assert m.module == "backup"
    assert m.secrets[0].service == "r2-access-key-id"
    assert m.soak.min_days == 7

def test_revoke_secret_must_be_declared():
    bad = {**VALID, "uninstall": {**VALID["uninstall"], "revoke_secrets": ["undeclared-key"]}}
    with pytest.raises(ManifestError, match="revoke_secrets references undeclared secret"):
        ModuleManifest.model_validate(bad)

def test_generate_enum_rejects_unknown():
    bad = {**VALID, "secrets": [{"account": "sanctum", "service": "x", "required": True, "generate": "rsa"}]}
    with pytest.raises(Exception):
        ModuleManifest.model_validate(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_module_manifest.py -q`
Expected: FAIL (ImportError: cannot import name 'ModuleManifest').

- [ ] **Step 3: Implement the models**

```python
# sanctum_cli/modules/manifest.py
"""Declarative module manifest — the contract a Sanctum module ships.

A module describes the services it owns, the secrets it needs, its health
probes, its alert routing, its uninstall steps, docs, demo, and soak target.
The manifest is the boundary between a module and the spine; consumers
(doctor --ship, module commands, self-test) never reach past it.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator


class ManifestError(ValueError):
    """Raised when a manifest is structurally valid YAML but semantically invalid."""


class ServiceKind(str, Enum):
    launchagent = "launchagent"
    launchdaemon = "launchdaemon"


class SecretGenerate(str, Enum):
    hex64 = "hex64"   # mint 64 hex chars on install if absent
    none = "none"     # operator must supply (e.g. cloud keys); never copied from Bert


class ServiceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    kind: ServiceKind = ServiceKind.launchagent
    keepalive: bool = False
    health_probe: str | None = None   # dotted path into the probe registry


class SecretSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = "sanctum"
    service: str
    required: bool = True
    generate: SecretGenerate = SecretGenerate.none


class PagerCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    probe: str
    severity: str  # p0 | p1 | p2


class AlertSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink: str = "chitti"
    pager_conditions: list[PagerCondition] = []


class UninstallSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bootout_labels: list[str] = []
    revoke_secrets: list[str] = []     # service names (must be declared in secrets)
    remove_paths: list[str] = []       # only removed under --purge
    rename_suffix: str = ".uninstalled-{date}"


class SoakSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_days: int = 7
    result_path: str = "~/.sanctum/soak/{module}.json"


class ModuleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module: str
    version: str
    description: str
    depends_on: list[str] = []
    services: list[ServiceSpec] = []
    secrets: list[SecretSpec] = []
    probes: list[str] = []
    alerts: AlertSpec = AlertSpec()
    uninstall: UninstallSpec = UninstallSpec()
    docs: str
    demo: str
    soak: SoakSpec = SoakSpec()

    @model_validator(mode="after")
    def _check_revoke_secrets_declared(self) -> "ModuleManifest":
        declared = {s.service for s in self.secrets}
        for svc in self.uninstall.revoke_secrets:
            if svc not in declared:
                raise ManifestError(
                    f"uninstall.revoke_secrets references undeclared secret '{svc}' "
                    f"(declared: {sorted(declared)})"
                )
        return self


def load_manifest(path: Path) -> ModuleManifest:
    """Load + validate a manifest YAML file. Raises ManifestError on bad data."""
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ManifestError(f"cannot read manifest {path}: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"manifest {path} is not a mapping")
    return ModuleManifest.model_validate(data)
```

Add to `sanctum_cli/modules/__init__.py`:

```python
from sanctum_cli.modules.manifest import (
    ManifestError,
    ModuleManifest,
    load_manifest,
)

__all__ = ["ManifestError", "ModuleManifest", "load_manifest"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_module_manifest.py -q && uv run mypy sanctum_cli/modules && uv run ruff check sanctum_cli/modules`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add sanctum_cli/modules/ tests/test_module_manifest.py
git commit -m "feat(modules): module manifest pydantic models + loader

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — Module Registry

### Task 2: Discover + resolve manifests

**Files:**
- Create: `sanctum_cli/modules/registry.py`
- Test: `tests/test_module_registry.py`

Behavior: `ModuleRegistry.discover()` loads every `*.module.yaml` in `sanctum_cli/modules/builtins/` (packaged) and `~/.sanctum/modules/` (user; user overrides built-in by `module` name). `resolve_order()` returns modules topologically sorted by `depends_on`, raising `ManifestError` on a missing dependency or a cycle.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_module_registry.py
import pytest
from sanctum_cli.modules.manifest import ModuleManifest
from sanctum_cli.modules.registry import ModuleRegistry, DependencyError

def _m(name, deps=None):
    return ModuleManifest.model_validate({
        "module": name, "version": "1.0.0", "description": name,
        "depends_on": deps or [], "docs": "https://x.invalid", "demo": "true",
    })

def test_resolve_order_topological():
    reg = ModuleRegistry(manifests={"a": _m("a", ["b"]), "b": _m("b")})
    assert reg.resolve_order() == ["b", "a"]

def test_missing_dependency_raises():
    reg = ModuleRegistry(manifests={"a": _m("a", ["ghost"])})
    with pytest.raises(DependencyError, match="ghost"):
        reg.resolve_order()

def test_cycle_raises():
    reg = ModuleRegistry(manifests={"a": _m("a", ["b"]), "b": _m("b", ["a"])})
    with pytest.raises(DependencyError, match="cycle"):
        reg.resolve_order()

def test_user_overrides_builtin(tmp_path, monkeypatch):
    # a user manifest with the same `module` name wins over the built-in
    (tmp_path / "backup.module.yaml").write_text(
        "module: backup\nversion: 9.9.9\ndescription: user\ndocs: https://x.invalid\ndemo: true\n")
    monkeypatch.setenv("SANCTUM_MODULES_DIR", str(tmp_path))
    reg = ModuleRegistry.discover()
    assert reg.get("backup").version == "9.9.9"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_module_registry.py -q`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement the registry**

```python
# sanctum_cli/modules/registry.py
from __future__ import annotations

import os
from pathlib import Path

from sanctum_cli.modules.manifest import ManifestError, ModuleManifest, load_manifest

BUILTIN_DIR = Path(__file__).parent / "builtins"


class DependencyError(ManifestError):
    """Missing dependency or dependency cycle in the module graph."""


def _user_dir() -> Path:
    return Path(os.environ.get("SANCTUM_MODULES_DIR",
                               os.path.expanduser("~/.sanctum/modules")))


class ModuleRegistry:
    def __init__(self, manifests: dict[str, ModuleManifest]):
        self.manifests = manifests

    @classmethod
    def discover(cls) -> "ModuleRegistry":
        found: dict[str, ModuleManifest] = {}
        for d in (BUILTIN_DIR, _user_dir()):   # user dir second => overrides
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.module.yaml")):
                m = load_manifest(f)
                found[m.module] = m
        return cls(found)

    def get(self, name: str) -> ModuleManifest:
        if name not in self.manifests:
            raise ManifestError(f"unknown module '{name}' "
                                f"(installed: {sorted(self.manifests)})")
        return self.manifests[name]

    def names(self) -> list[str]:
        return sorted(self.manifests)

    def resolve_order(self) -> list[str]:
        order: list[str] = []
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(name: str) -> None:
            if name in done:
                return
            if name in visiting:
                raise DependencyError(f"dependency cycle through '{name}'")
            if name not in self.manifests:
                raise DependencyError(f"missing dependency '{name}'")
            visiting.add(name)
            for dep in self.manifests[name].depends_on:
                visit(dep)
            visiting.discard(name)
            done.add(name)
            order.append(name)

        for n in sorted(self.manifests):
            visit(n)
        return order
```

Append to `__init__.py`: `from sanctum_cli.modules.registry import ModuleRegistry, DependencyError` and extend `__all__`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_module_registry.py -q && uv run mypy sanctum_cli/modules`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sanctum_cli/modules/registry.py sanctum_cli/modules/__init__.py tests/test_module_registry.py
git commit -m "feat(modules): registry with discovery + topological dep resolution

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — Backup reference manifest

### Task 3: Ship the backup built-in manifest + contract test

**Files:**
- Create: `sanctum_cli/modules/builtins/backup.module.yaml`
- Test: `tests/test_backup_manifest.py`

The backup module describes the CLI's own backup job: no LaunchAgent (backup runs via `sanctum backup`/cron), the R2 cloud-cred secrets (operator-supplied — `generate: none`), a `backup_recent` probe, and uninstall that revokes the cloud creds.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backup_manifest.py
from sanctum_cli.modules.registry import ModuleRegistry

def test_backup_manifest_is_builtin_and_valid():
    reg = ModuleRegistry.discover()
    assert "backup" in reg.names()
    m = reg.get("backup")
    # every revoke target is a declared secret (contract enforced by the model,
    # but assert here against the on-disk file as a different-author check)
    declared = {s.service for s in m.secrets}
    assert set(m.uninstall.revoke_secrets) <= declared
    # cloud keys are operator-supplied, never auto-generated/copied
    assert all(s.generate.value == "none" for s in m.secrets)
    # docs + demo are present (the docs+demo gate needs them)
    assert m.docs and m.demo
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_backup_manifest.py -q`
Expected: FAIL (assert "backup" in names — file doesn't exist yet).

- [ ] **Step 3: Write the manifest**

```yaml
# sanctum_cli/modules/builtins/backup.module.yaml
module: backup
version: 1.0.0
description: Encrypted restic backup to the operator's own cloud repo (R2 default)
depends_on: []
services: []
secrets:
  - {account: sanctum, service: r2-account-id, required: true, generate: none}
  - {account: sanctum, service: r2-access-key-id, required: true, generate: none}
  - {account: sanctum, service: r2-secret-access-key, required: true, generate: none}
probes:
  - backup.backup_recent
alerts:
  sink: chitti
  pager_conditions: []
uninstall:
  bootout_labels: []
  revoke_secrets: [r2-account-id, r2-access-key-id, r2-secret-access-key]
  remove_paths: []
  rename_suffix: ".uninstalled-{date}"
docs: https://github.com/Ogilthorp3/sanctum-cli/blob/main/docs/backup.md
demo: sanctum backup snapshots
soak:
  min_days: 7
  result_path: ~/.sanctum/soak/backup.json
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_backup_manifest.py -q`
Expected: PASS. Also confirm packaging includes yaml: add `"*.module.yaml"` to package-data/`[tool.hatch.build]` if needed so `BUILTIN_DIR.glob` works from an installed wheel — verify with `uv run python -c "from sanctum_cli.modules.registry import ModuleRegistry as R; print(R.discover().names())"`.

- [ ] **Step 5: Commit**

```bash
git add sanctum_cli/modules/builtins/backup.module.yaml tests/test_backup_manifest.py pyproject.toml
git commit -m "feat(modules): backup reference module manifest

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 4 — Ship gates (pure functions)

### Task 4: GateResult + the six gate functions

**Files:**
- Create: `sanctum_cli/ship_gates.py`
- Test: `tests/test_ship_gates.py`

Each gate is a pure-ish function `(manifest, deps) -> GateResult`. Side-effecting lookups (keychain, http) are injected as callables so gates are unit-testable with fakes (Contracts at the Boundary: test the hostile input, not the happy path).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ship_gates.py
from sanctum_cli.modules.manifest import ModuleManifest
from sanctum_cli.ship_gates import (
    GateStatus, gate_secrets, gate_alert_hygiene, gate_docs_demo,
)

def _m(**over):
    base = {"module": "m", "version": "1.0.0", "description": "m",
            "docs": "https://x.invalid", "demo": "true"}
    base.update(over)
    return ModuleManifest.model_validate(base)

def test_secrets_red_when_missing():
    m = _m(secrets=[{"service": "k", "required": True, "generate": "none"}])
    r = gate_secrets(m, keychain_has=lambda a, s: False, is_default=lambda a, s: False)
    assert r.status is GateStatus.RED

def test_secrets_amber_when_default_looking():
    m = _m(secrets=[{"service": "k", "required": True, "generate": "none"}])
    r = gate_secrets(m, keychain_has=lambda a, s: True, is_default=lambda a, s: True)
    assert r.status is GateStatus.AMBER

def test_alert_hygiene_red_on_dead_sink():
    m = _m(alerts={"sink": "chitti", "pager_conditions": []})
    r = gate_alert_hygiene(m, sink_live=lambda name: False, probe_is_false_green=lambda p: False)
    assert r.status is GateStatus.RED
    assert "sink" in r.detail.lower()

def test_alert_hygiene_red_on_false_green_probe():
    m = _m(probes=["m.p"], alerts={"sink": "chitti", "pager_conditions": []})
    r = gate_alert_hygiene(m, sink_live=lambda n: True, probe_is_false_green=lambda p: p == "m.p")
    assert r.status is GateStatus.RED

def test_docs_demo_green_when_both_present():
    m = _m()
    r = gate_docs_demo(m, docs_resolves=lambda u: True, demo_exits_zero=lambda c: True)
    assert r.status is GateStatus.GREEN
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_ship_gates.py -q`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement gates**

```python
# sanctum_cli/ship_gates.py
"""Pure ship-bar gate functions. Side effects are injected so each gate is
unit-testable against hostile inputs (dead sink, false-green probe, missing
secret) without touching the real haus."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from sanctum_cli.modules.manifest import ModuleManifest


class GateStatus(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass
class GateResult:
    name: str
    status: GateStatus
    detail: str


def gate_install_uninstall(m: ModuleManifest) -> GateResult:
    if not m.uninstall.rename_suffix:
        return GateResult("install/uninstall", GateStatus.RED, "no uninstall handler")
    if m.uninstall.remove_paths and not m.uninstall.rename_suffix:
        return GateResult("install/uninstall", GateStatus.AMBER, "purge without rename safety")
    return GateResult("install/uninstall", GateStatus.GREEN, "reversible, data preserved")


def gate_secrets(
    m: ModuleManifest,
    keychain_has: Callable[[str, str], bool],
    is_default: Callable[[str, str], bool],
) -> GateResult:
    missing = [s.service for s in m.secrets
               if s.required and not keychain_has(s.account, s.service)]
    if missing:
        return GateResult("secrets-bootstrap", GateStatus.RED,
                          f"missing required secrets: {missing}")
    defaulted = [s.service for s in m.secrets if is_default(s.account, s.service)]
    if defaulted:
        return GateResult("secrets-bootstrap", GateStatus.AMBER,
                          f"secrets look like Bert-defaults: {defaulted}")
    return GateResult("secrets-bootstrap", GateStatus.GREEN, "present + non-default")


def gate_self_heal(
    m: ModuleManifest,
    heal_action_ok: Callable[[str], bool],
) -> GateResult:
    keepalive = [s for s in m.services if s.keepalive]
    no_probe = [s.label for s in keepalive if not s.health_probe]
    if no_probe:
        return GateResult("self-heal", GateStatus.RED,
                          f"keepalive services without a health probe: {no_probe}")
    crashing = [s.label for s in keepalive
                if s.health_probe and not heal_action_ok(s.label)]
    if crashing:
        return GateResult("self-heal", GateStatus.RED,
                          f"heal action missing/crashing: {crashing}")
    if not keepalive:
        return GateResult("self-heal", GateStatus.GREEN, "no long-running services")
    return GateResult("self-heal", GateStatus.AMBER, "heal wired, soak-unproven")


def gate_alert_hygiene(
    m: ModuleManifest,
    sink_live: Callable[[str], bool],
    probe_is_false_green: Callable[[str], bool],
) -> GateResult:
    if not sink_live(m.alerts.sink):
        return GateResult("alert-hygiene", GateStatus.RED,
                          f"alert sink '{m.alerts.sink}' is not reachable")
    liars = [p for p in m.probes if probe_is_false_green(p)]
    if liars:
        return GateResult("alert-hygiene", GateStatus.RED,
                          f"false-green probes (report ok while failing): {liars}")
    if len(m.alerts.pager_conditions) > 3:
        return GateResult("alert-hygiene", GateStatus.AMBER,
                          "pager conditions look broad; keep P0/P1 crucial-only")
    return GateResult("alert-hygiene", GateStatus.GREEN, "live sink, minimal pager")


def gate_soak(
    m: ModuleManifest,
    soak_days: Callable[[ModuleManifest], float | None],
    soak_clean: Callable[[ModuleManifest], bool],
) -> GateResult:
    days = soak_days(m)
    if days is None:
        return GateResult("soak", GateStatus.RED, "no soak result recorded")
    if not soak_clean(m):
        return GateResult("soak", GateStatus.RED, f"soak recorded faults ({days:.1f}d)")
    if days < m.soak.min_days:
        return GateResult("soak", GateStatus.AMBER,
                          f"soak {days:.1f}d < required {m.soak.min_days}d")
    return GateResult("soak", GateStatus.GREEN, f"clean {days:.1f}d soak")


def gate_docs_demo(
    m: ModuleManifest,
    docs_resolves: Callable[[str], bool],
    demo_exits_zero: Callable[[str], bool],
) -> GateResult:
    ok_docs = docs_resolves(m.docs)
    ok_demo = demo_exits_zero(m.demo)
    if ok_docs and ok_demo:
        return GateResult("docs+demo", GateStatus.GREEN, "docs resolve, demo exits 0")
    if ok_docs or ok_demo:
        return GateResult("docs+demo", GateStatus.AMBER,
                          f"docs={'ok' if ok_docs else 'X'} demo={'ok' if ok_demo else 'X'}")
    return GateResult("docs+demo", GateStatus.RED, "neither docs nor demo verified")


def overall(results: list[GateResult]) -> GateStatus:
    if any(r.status is GateStatus.RED for r in results):
        return GateStatus.RED
    if any(r.status is GateStatus.AMBER for r in results):
        return GateStatus.AMBER
    return GateStatus.GREEN
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_ship_gates.py -q && uv run mypy sanctum_cli/ship_gates.py && uv run ruff check sanctum_cli/ship_gates.py`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add sanctum_cli/ship_gates.py tests/test_ship_gates.py
git commit -m "feat(ship): pure ship-bar gate functions with injected side effects

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 5 — `sanctum doctor --ship <module>`

### Task 5: Wire gates + registry + real side-effect adapters + render

**Files:**
- Create: `sanctum_cli/commands/ship.py`
- Modify: `sanctum_cli/cli.py` (add `--ship NAME` option to the existing `doctor` command; if set, dispatch to ship evaluator)
- Test: `tests/test_doctor_ship.py`

Real adapters (live in `ship.py`): `keychain_has` → `keychain.exists`; `is_default` → compare a fingerprint of the value against a small set of known Bert-default fingerprints (start with: empty set → always False, with a TODO-free comment that the fingerprint list is intentionally conservative); `sink_live` → for `chitti`, GET `http://127.0.0.1:2188/health` with a 2s timeout; `probe_is_false_green` → start conservative: False for all (a later task can add the jsonl-only-failure heuristic); `docs_resolves` → HEAD the URL (2s) or accept local `docs/*.md` path existence; `demo_exits_zero` → run the demo command with a short timeout and check rc==0; `soak_days`/`soak_clean` → read the soak result file (Phase 6).

- [ ] **Step 1: Write the failing test** (uses a fake registry + injected adapters via a thin `evaluate()` seam)

```python
# tests/test_doctor_ship.py
from sanctum_cli.modules.manifest import ModuleManifest
from sanctum_cli.modules.registry import ModuleRegistry
from sanctum_cli.ship_gates import GateStatus
from sanctum_cli.commands.ship import evaluate

def _reg():
    m = ModuleManifest.model_validate({
        "module": "backup", "version": "1.0.0", "description": "b",
        "secrets": [{"service": "k", "required": True, "generate": "none"}],
        "docs": "https://x.invalid", "demo": "true",
    })
    return ModuleRegistry(manifests={"backup": m})

def test_evaluate_red_when_secret_missing_and_sink_dead():
    res = evaluate("backup", registry=_reg(), adapters={
        "keychain_has": lambda a, s: False,
        "is_default": lambda a, s: False,
        "heal_action_ok": lambda l: True,
        "sink_live": lambda n: False,
        "probe_is_false_green": lambda p: False,
        "soak_days": lambda m: None,
        "soak_clean": lambda m: False,
        "docs_resolves": lambda u: True,
        "demo_exits_zero": lambda c: True,
    })
    assert res.verdict is GateStatus.RED
    names = {g.name for g in res.gates if g.status is GateStatus.RED}
    assert {"secrets-bootstrap", "alert-hygiene", "soak"} <= names

def test_evaluate_green_when_all_pass():
    res = evaluate("backup", registry=_reg(), adapters={
        "keychain_has": lambda a, s: True, "is_default": lambda a, s: False,
        "heal_action_ok": lambda l: True, "sink_live": lambda n: True,
        "probe_is_false_green": lambda p: False,
        "soak_days": lambda m: 8.0, "soak_clean": lambda m: True,
        "docs_resolves": lambda u: True, "demo_exits_zero": lambda c: True,
    })
    assert res.verdict is GateStatus.GREEN
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_doctor_ship.py -q` → FAIL (ImportError).

- [ ] **Step 3: Implement `evaluate()` + a default-adapters builder + Rich render**

Implement in `sanctum_cli/commands/ship.py`:
- `@dataclass ShipReport: module: str; gates: list[GateResult]; verdict: GateStatus`
- `def evaluate(module, registry, adapters) -> ShipReport` — calls the six gates with the adapters, computes `overall()`.
- `def default_adapters() -> dict[str, Callable]` — the real side-effect adapters described above (keychain.exists, chitti /health probe, HEAD docs, run demo with `subprocess.run(..., timeout=20)`).
- `def render(report, json_out: bool) -> int` — Rich table (gate, status colored green/yellow/red, detail) + a verdict line; return exit code `0` if verdict != RED else `1`.
- In `cli.py`, extend the existing `doctor` command with `ship: str | None = typer.Option(None, "--ship", help="Score a module against the ship bar")`; when set: `report = evaluate(ship, ModuleRegistry.discover(), default_adapters()); raise typer.Exit(render(report, json_out))`. Leave the existing doctor behavior unchanged when `--ship` is absent.

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/test_doctor_ship.py -q && uv run mypy sanctum_cli && uv run pytest -q`
Expected: PASS. Manual smoke: `uv run sanctum doctor --ship backup` renders a gate table (will show RED on secrets/soak in a dev env without R2 keys — that's correct).

- [ ] **Step 5: Commit**

```bash
git add sanctum_cli/commands/ship.py sanctum_cli/cli.py tests/test_doctor_ship.py
git commit -m "feat(ship): sanctum doctor --ship scores a module against the bar

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 6 — Soak harness

### Task 6: SoakResult schema + classifier + minimal runner

**Files:**
- Create: `sanctum_cli/soak/__init__.py`, `sanctum_cli/soak/harness.py`
- Modify: `sanctum_cli/cli.py` (add `sanctum soak <module> [--days 7] [--interval-sec 3600] [--once]`)
- Test: `tests/test_soak_harness.py`

`SoakResult` = `{module, started_at, last_at, samples: list[Sample], faults: list[str]}` written to the module's `soak.result_path`. A `Sample` records `{ts, pressure_level:int, swap_used_mb:int, red_probes:list[str], service_nonzero:list[str]}`. `classify_soak(result) -> (days: float, clean: bool)`: clean iff zero `faults`, no sample with `red_probes`, no sample with `service_nonzero`, and no `pressure_level==4` without a later recovery sample. The runner appends a sample each interval; `--once` does one sample (for tests/cron). `--days` sets the target the gate compares against.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_soak_harness.py
from sanctum_cli.soak.harness import SoakResult, Sample, classify_soak

def _r(samples, faults=None):
    return SoakResult(module="m", started_at="2026-06-01T00:00:00Z",
                      last_at="2026-06-08T00:00:00Z", samples=samples, faults=faults or [])

def test_clean_seven_day_soak():
    s = [Sample(ts="t", pressure_level=1, swap_used_mb=100, red_probes=[], service_nonzero=[])]
    days, clean = classify_soak(_r(s))
    assert clean is True and days >= 7.0

def test_red_probe_marks_dirty():
    s = [Sample(ts="t", pressure_level=1, swap_used_mb=100, red_probes=["m.p"], service_nonzero=[])]
    _, clean = classify_soak(_r(s))
    assert clean is False

def test_service_nonzero_marks_dirty():
    s = [Sample(ts="t", pressure_level=1, swap_used_mb=100, red_probes=[], service_nonzero=["com.sanctum.x"])]
    _, clean = classify_soak(_r(s))
    assert clean is False

def test_unrecovered_critical_pressure_marks_dirty():
    s = [Sample(ts="t1", pressure_level=4, swap_used_mb=9000, red_probes=[], service_nonzero=[])]
    _, clean = classify_soak(_r(s))
    assert clean is False  # critical pressure never followed by a normal sample
```

- [ ] **Step 2: Run to verify it fails** — FAIL (ImportError).

- [ ] **Step 3: Implement** `SoakResult`/`Sample` as pydantic models, `classify_soak()` per the rules above (days = (last_at - started_at) in days; clean per the four conditions — for critical pressure, "recovery" = a strictly-later sample with `pressure_level <= 2`), `run_soak(module, registry, days, interval, once)` that builds a `Sample` from live signals (`sysctl kern.memorystatus_vm_pressure_level`, `sysctl vm.swapusage`, the module's probes via the registry, `launchctl list` exit codes for the module's `services`), appends to the result file (atomic write), and loops on interval unless `--once`. Export from `__init__.py`.

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/test_soak_harness.py -q && uv run mypy sanctum_cli/soak`
Expected: PASS. Smoke: `uv run sanctum soak backup --once` writes `~/.sanctum/soak/backup.json` with one sample.

- [ ] **Step 5: Commit**

```bash
git add sanctum_cli/soak/ sanctum_cli/cli.py tests/test_soak_harness.py
git commit -m "feat(soak): soak harness — sample, classifier, runner

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Then wire `ship.py`'s `soak_days`/`soak_clean` adapters to read the result file via `classify_soak`, and add a test that `gate_soak` goes GREEN against a written clean result. Commit that as a small follow-up.

---

## Phase 7 — `sanctum module` commands

### Task 7: `module list` + `module status` (read-only first)

**Files:**
- Create: `sanctum_cli/commands/module.py`
- Modify: `sanctum_cli/cli.py` (register `module` Typer sub-app)
- Test: `tests/test_module_commands.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_module_commands.py
from typer.testing import CliRunner
from sanctum_cli.cli import app

runner = CliRunner()

def test_module_list_shows_backup():
    r = runner.invoke(app, ["module", "list"])
    assert r.exit_code == 0
    assert "backup" in r.stdout

def test_module_status_unknown_errors():
    r = runner.invoke(app, ["module", "status", "ghost"])
    assert r.exit_code != 0
    assert "unknown module" in r.stdout.lower() or "ghost" in r.stdout
```

- [ ] **Step 2: Run to verify it fails** — FAIL.

- [ ] **Step 3: Implement** `module list` (Rich table: module, version, description, last ship verdict if cached) and `module status <name>` (services, each declared secret present?/missing via `keychain.exists`, probes, soak age via `classify_soak` if a result exists). Use `ModuleRegistry.discover()`. On unknown module, print the `ManifestError` message and `raise typer.Exit(2)`. Register the sub-app in `cli.py` as `app.add_typer(module_app, name="module")`.

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/test_module_commands.py -q`. Smoke: `uv run sanctum module list`, `uv run sanctum module status backup`.

- [ ] **Step 5: Commit** (`feat(modules): sanctum module list/status` + trailer).

### Task 8: `module install` / `uninstall` / `demo`

**Files:** Modify `sanctum_cli/commands/module.py`; Test: extend `tests/test_module_commands.py`.

- [ ] **Step 1:** Write failing tests against a temp `HOME`/`SANCTUM_MODULES_DIR` (real throwaway state, not mocks): `install` of a local manifest path registers it (idempotent — second run is a no-op) and bootstraps `generate: hex64` secrets via `keychain.rotate` (skip `generate: none`, printing what the operator must supply); `uninstall` runs the manifest's bootout/revoke/rename steps coordinating with the global uninstall logic; `demo` runs the manifest's demo command.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Reuse `commands/uninstall.py` helpers for bootout/rename/revoke so the teardown logic isn't duplicated (import them; if they're not importable as functions, refactor the shared steps into small functions in `uninstall.py` and call from both). `install` validates the manifest, copies it to `~/.sanctum/modules/`, mints `hex64` secrets if absent, and is idempotent.
- [ ] **Step 4:** Run → PASS. Smoke against a temp HOME.
- [ ] **Step 5:** Commit (`feat(modules): module install/uninstall/demo` + trailer).

---

## Phase 8 — PROBES module-keyed refactor

### Task 9: Make self-test probes module-aware

**Files:**
- Modify: `sanctum_cli/commands/self_test.py` (PROBES flat list → `dict[str, list[Probe]]` keyed by tier/module; merge probes contributed by installed modules)
- Test: `tests/test_probes_module_keyed.py`

- [ ] **Step 1:** Write a failing test asserting (a) the existing CLI-tier + haus-tier probes still run and `--only` filtering still works (regression guard against the current 12 probes), and (b) a module that declares `probes: ["backup.backup_recent"]` contributes that probe under its module key, discoverable by the runner.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Refactor: keep the existing probes under keys `"cli"` and `"haus"` (preserving the `_haus_only` tiering exactly), add a `module_probes(registry)` that maps each module's `probes` dotted-paths to `Probe` objects by importing the referenced callables, and have the runner iterate the merged dict. Preserve exit-code semantics and `--only`. **Do not change** the existing probe behavior — this is a pure restructure + extension.
- [ ] **Step 4:** Run → PASS, plus the full suite (`uv run pytest -q`) to prove no regression in `test` for self-test.
- [ ] **Step 5:** Commit (`refactor(self-test): module-keyed probe registry` + trailer).

---

## Phase 9 — Config + final wiring + e2e smoke

### Task 10: `CliConfig.modules` + end-to-end smoke

**Files:**
- Modify: `sanctum_cli/config.py` (add `modules: dict[str, ModuleConfig] = {}` to `CliConfig`, where `ModuleConfig` carries optional per-module overrides — at minimum `enabled: bool = True`; keep it minimal, YAGNI)
- Create: `tests/test_ship_e2e.py`
- Modify: `README.md` / `SPEC.md` (note the new `module` + `doctor --ship` + `soak` surface)

- [ ] **Step 1:** Write an e2e smoke test (Contracts at the Boundary — real artifact across real boundaries): in a temp HOME, write a clean soak result for `backup`, set the three R2 keychain entries to throwaway values (or inject adapters), run `evaluate("backup", ModuleRegistry.discover(), default_adapters())`, and assert the verdict and that each gate name appears. Then `runner.invoke(app, ["doctor", "--ship", "backup", "--json"])` and assert the JSON parses with a `verdict` field.
- [ ] **Step 2:** Run → FAIL (config field / json output not present).
- [ ] **Step 3:** Add `ModuleConfig` + `CliConfig.modules`; ensure `doctor --ship --json` emits machine output; update README/SPEC surface list.
- [ ] **Step 4:** Run the **whole** suite + linters: `uv run pytest -q && uv run mypy sanctum_cli && uv run ruff check .`. Expected: all green, no regression in the existing ~201 tests. Manual: `uv run sanctum doctor --ship backup`, `uv run sanctum module list`, `uv run sanctum soak backup --once`.
- [ ] **Step 5:** Commit (`feat(ship): config.modules + e2e smoke + docs surface` + trailer).

---

## Self-Review (run by the author before execution)

1. **Spec coverage:** manifest (Task 1), registry (Task 2), backup reference (Task 3), six gates (Task 4), `doctor --ship` (Task 5), soak harness (Task 6), `module` commands (Tasks 7–8), PROBES refactor (Task 9), config + e2e (Task 10) — every spec §4–§11 component maps to a task. The 6 faults in the spec's motivation map to gate tests (dead sink, false-green, missing secret, soak-dirty).
2. **Placeholder scan:** code blocks are complete for models, gates, classifier, and tests; command-render tasks specify exact behavior, signatures, adapters, and smoke commands. No "TBD"/"handle errors"/"similar to Task N".
3. **Type consistency:** `GateResult`/`GateStatus` used identically in Tasks 4–5; `ModuleManifest` field names match across manifest, registry, gates, backup yaml; `evaluate(module, registry, adapters)` and `default_adapters()` keys match the gate function signatures; `classify_soak` returns `(days, clean)` consumed by `gate_soak` adapters.
4. **Deferred (explicit, not silent):** `probe_is_false_green` ships conservative (always False) with the jsonl-only-failure heuristic noted for a follow-up; the Bert-default fingerprint set starts empty. These are amber-safe defaults, documented in Task 5, not hidden gaps.

## Execution

Subagent-driven (the council): fresh subagent per task, two-stage review between tasks. Branch `feat/ship-bar-spine`. Nothing pushed until Bert reviews.
