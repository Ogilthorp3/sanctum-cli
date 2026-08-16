"""``sanctum module`` — inspect, install, uninstall, and demo Sanctum modules.

``list``   — Rich table: module, version, description, last ship verdict (if a
             cached soak result exists, otherwise "-").

``status`` — Per-module detail: services, secrets (present/missing via
             ``keychain.exists``), probes, soak age (via ``classify_soak`` if
             the result file exists).

``install``   — Validate a manifest (by name or path), copy it into the user
                modules dir, mint ``generate: hex64`` secrets if absent, and
                print the ``generate: none`` secrets the operator must supply.
                Idempotent: a second run re-mints nothing and re-copies nothing.

``uninstall`` — Run the manifest's teardown (bootout labels, revoke secrets,
                rename the installed manifest). ``--purge`` also removes the
                declared ``remove_paths``.

``demo``      — Run the manifest's demo command.

SAFETY: every destructive side effect (keychain mint/revoke, launchd bootout,
filesystem rename/remove) is an INJECTED callable. The command layer passes the
real adapters (``keychain``-backed mint, the shared ``uninstall.py`` teardown
primitives); tests pass fakes that record calls. The core ``install_module`` /
``uninstall_module`` / ``demo_module`` functions never reach the real Keychain
or launchd on their own — they only call what they're handed.

On unknown module name, prints the ``ManifestError`` message and exits with
code 2.
"""

from __future__ import annotations

import os
import secrets as _secrets
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from sanctum_cli import keychain
from sanctum_cli.commands import uninstall as _uninstall
from sanctum_cli.modules.manifest import (
    ManifestError,
    ModuleManifest,
    SecretGenerate,
    load_manifest,
)
from sanctum_cli.modules.registry import ModuleRegistry
from sanctum_cli.soak import SoakResult, classify_soak

if TYPE_CHECKING:
    from collections.abc import Callable

module_app = typer.Typer(help="Inspect, install, and remove Sanctum modules.")

console = Console()

from sanctum_cli.keychain import SECURITY_BIN


def _load_soak_result(module: str, result_path_template: str) -> SoakResult | None:
    """Load the soak result file for *module*, or return None if absent/corrupt."""
    result_path = Path(result_path_template.replace("{module}", module)).expanduser()
    if not result_path.is_file():
        return None
    try:
        return SoakResult.model_validate_json(result_path.read_text())
    except (ValueError, OSError):
        return None


def _soak_summary(module: str, result_path_template: str) -> str:
    """Return a human-readable soak age + status string, or '-' if not recorded."""
    result = _load_soak_result(module, result_path_template)
    if result is None:
        return "-"
    days, clean = classify_soak(result)
    status = "clean" if clean else "dirty"
    return f"{days:.1f}d {status}"


@module_app.command("list", help="List all installed modules with version and description.")
def list_modules() -> None:
    """Render a Rich table of all discovered modules."""
    registry = ModuleRegistry.discover()
    names = registry.names()

    t = Table(
        title="Sanctum modules",
        show_header=True,
        header_style="bold",
    )
    t.add_column("module", no_wrap=True)
    t.add_column("version", no_wrap=True)
    t.add_column("description")
    t.add_column("soak", no_wrap=True)

    for name in names:
        m = registry.get(name)
        soak_col = _soak_summary(name, m.soak.result_path)
        t.add_row(name, m.version, m.description, soak_col)

    console.print(t)


@module_app.command("status", help="Show detail for one installed module.")
def module_status(
    name: Annotated[str, typer.Argument(help="Module name, e.g. backup.")],
) -> None:
    """Show services, secrets, probes, and soak age for *name*."""
    registry = ModuleRegistry.discover()
    try:
        m = registry.get(name)
    except ManifestError as exc:
        console.print(str(exc))
        raise typer.Exit(2) from exc

    console.print(f"[bold]Module:[/] {m.module}  [dim]v{m.version}[/]")
    console.print(f"[dim]{m.description}[/]")
    console.print()

    # ── services ──────────────────────────────────────────────────────
    if m.services:
        console.print("[bold]Services[/]")
        for svc in m.services:
            keepalive_note = " [keepalive]" if svc.keepalive else ""
            console.print(f"  {svc.label}  ({svc.kind.value}){keepalive_note}")
    else:
        console.print("[bold]Services:[/] [dim]none declared[/]")
    console.print()

    # ── secrets ───────────────────────────────────────────────────────
    if m.secrets:
        console.print("[bold]Secrets[/]")
        for sec in m.secrets:
            present = keychain.exists(sec.account, sec.service)
            mark = Text("present", style="green") if present else Text("missing", style="red")
            req_note = "" if sec.required else " [dim](optional)[/dim]"
            console.print(
                Text.assemble(
                    Text(f"  {sec.service}  "),
                    mark,
                    Text(req_note),
                )
            )
    else:
        console.print("[bold]Secrets:[/] [dim]none declared[/]")
    console.print()

    # ── probes ────────────────────────────────────────────────────────
    if m.probes:
        console.print("[bold]Probes[/]")
        for probe in m.probes:
            console.print(f"  {probe}")
    else:
        console.print("[bold]Probes:[/] [dim]none declared[/]")
    console.print()

    # ── soak ──────────────────────────────────────────────────────────
    soak_summary = _soak_summary(m.module, m.soak.result_path)
    console.print(f"[bold]Soak:[/] {soak_summary}  [dim](target: {m.soak.min_days}d)[/]")
    console.print(f"[dim]docs: {m.docs}[/]")
    console.print(f"[dim]demo: {m.demo}[/]")


# ─── install / uninstall / demo — result types ───────────────────────


@dataclass
class InstallResult:
    """Outcome of an install.

    ``minted``        — service names of secrets newly minted this run.
    ``must_supply``   — service names of ``generate: none`` secrets the
                        operator must supply themselves (never auto-minted).
    ``already_installed`` — True iff the manifest file was already present
                        with identical content (the copy was a no-op).
    """

    name: str
    minted: list[str] = field(default_factory=list)
    must_supply: list[str] = field(default_factory=list)
    already_installed: bool = False


@dataclass
class UninstallResult:
    """Outcome of an uninstall — the labels booted, secrets revoked, paths
    renamed, and (under --purge) paths removed."""

    name: str
    bootout: list[str] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


# ─── core: install ───────────────────────────────────────────────────


def _user_modules_dir() -> Path:
    """The directory installed manifests live in (env override for tests)."""
    return Path(os.environ.get("SANCTUM_MODULES_DIR", Path("~/.sanctum/modules").expanduser()))


def _resolve_manifest(source: str) -> ModuleManifest:
    """Resolve *source* to a validated manifest.

    A *source* that names an existing file (or ends in ``.module.yaml``) is
    loaded from disk; otherwise it is treated as a module name and looked up
    in the discovered registry. Raises ``ManifestError`` on bad data or an
    unknown name.
    """
    p = Path(source).expanduser()
    if p.is_file() or source.endswith(".module.yaml"):
        return load_manifest(p)
    return ModuleRegistry.discover().get(source)


def install_module(
    source: str,
    *,
    modules_dir: Path | None = None,
    keychain_has: Callable[[str, str], bool],
    keychain_mint: Callable[[str, str], None],
) -> InstallResult:
    """Install a module: validate, copy its manifest, mint hex64 secrets.

    All destructive side effects are injected:
      * ``keychain_has(account, service) -> bool`` — does the secret exist?
      * ``keychain_mint(account, service) -> None`` — mint a fresh secret.

    Idempotent: a second run with the same on-disk manifest re-copies nothing
    and mints nothing (``generate: hex64`` secrets present per ``keychain_has``
    are skipped). ``generate: none`` secrets are NEVER minted — they are
    operator-supplied and surfaced in ``InstallResult.must_supply``.
    """
    manifest = _resolve_manifest(source)
    target_dir = modules_dir if modules_dir is not None else _user_modules_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{manifest.module}.module.yaml"

    # Serialize from the validated model so the on-disk copy is canonical and
    # round-trips through load_manifest (a different-author check vs. the raw
    # source bytes).
    import yaml as _yaml

    rendered = _yaml.safe_dump(
        manifest.model_dump(mode="json"), sort_keys=False, default_flow_style=False
    )
    already = dest.is_file() and dest.read_text() == rendered
    if not already:
        dest.write_text(rendered, encoding="utf-8")

    minted: list[str] = []
    must_supply: list[str] = []
    for sec in manifest.secrets:
        if sec.generate is SecretGenerate.none:
            if not keychain_has(sec.account, sec.service):
                must_supply.append(sec.service)
            continue
        # generate: hex64 — mint iff absent (idempotent)
        if not keychain_has(sec.account, sec.service):
            keychain_mint(sec.account, sec.service)
            minted.append(sec.service)

    return InstallResult(
        name=manifest.module,
        minted=minted,
        must_supply=must_supply,
        already_installed=already,
    )


# ─── core: uninstall ─────────────────────────────────────────────────


def uninstall_module(
    name: str,
    *,
    purge: bool,
    registry: ModuleRegistry,
    modules_dir: Path | None = None,
    bootout_label: Callable[[str], None],
    revoke_secret: Callable[[str, str], bool],
    rename_path: Callable[[Path, str], bool],
    remove_path: Callable[[Path], bool],
) -> UninstallResult:
    """Run a module's manifest teardown via injected callables.

    Steps (coordinating with the global ``sanctum uninstall`` primitives,
    which are the real adapters):
      1. bootout every ``uninstall.bootout_labels`` entry
      2. revoke every ``uninstall.revoke_secrets`` entry (account from the
         matching SecretSpec, defaulting to ``sanctum``)
      3. rename the installed manifest file out of the modules dir
      4. (``--purge`` only) remove every ``uninstall.remove_paths`` entry

    Injected callables let tests assert against recorded calls without
    touching the real Keychain or launchd. Raises ``ManifestError`` for an
    unknown module.
    """
    manifest = registry.get(name)
    result = UninstallResult(name=name)

    # 1. bootout labels — resolve the launchd domain per service kind.
    # Build a {label: kind} map from the manifest's declared services so we
    # can pick "system" for launchdaemons and "gui/<uid>" for everything else.
    label_to_kind = {svc.label: svc.kind for svc in manifest.services}
    for label in manifest.uninstall.bootout_labels:
        from sanctum_cli.modules.manifest import ServiceKind

        kind = label_to_kind.get(label)
        domain = "system" if kind is ServiceKind.launchdaemon else f"gui/{os.getuid()}"
        # Pass the full launchctl target string (domain/label) to the injected
        # callable so tests can assert the correct domain without touching launchd.
        bootout_label(f"{domain}/{label}")
        result.bootout.append(label)

    # 2. revoke secrets — resolve each service's account from its SecretSpec
    account_for = {s.service: s.account for s in manifest.secrets}
    for service in manifest.uninstall.revoke_secrets:
        account = account_for.get(service, "sanctum")
        revoke_secret(account, service)
        result.revoked.append(service)

    # 3. rename the installed manifest file (recoverable)
    target_dir = modules_dir if modules_dir is not None else _user_modules_dir()
    installed = target_dir / f"{manifest.module}.module.yaml"
    if installed.is_file() and rename_path(installed, manifest.uninstall.rename_suffix):
        result.renamed.append(str(installed))

    # 4. purge-only: remove declared paths
    if purge:
        for raw in manifest.uninstall.remove_paths:
            p = Path(raw).expanduser()
            if remove_path(p):
                result.removed.append(str(p))

    return result


# ─── core: demo ──────────────────────────────────────────────────────


def demo_module(
    name: str,
    *,
    registry: ModuleRegistry,
    run_demo: Callable[[str], int],
) -> int:
    """Run a module's demo command via the injected ``run_demo`` callable.

    Returns the demo's exit code. Raises ``ManifestError`` for an unknown
    module.
    """
    manifest = registry.get(name)
    return run_demo(manifest.demo)


# ─── real side-effect adapters (production) ──────────────────────────


def _bootout_full_target(target: str) -> None:
    """Bootout a launchd service by its full target string (``domain/label``).

    The injected ``bootout_label`` callable in ``uninstall_module`` receives
    the pre-resolved ``domain/label`` string so tests can assert domain
    correctness without touching launchd.  This is the real production adapter
    that converts that string to the ``launchctl bootout`` invocation.
    """
    subprocess.run(
        ["launchctl", "bootout", target],
        capture_output=True,
        text=True,
        check=False,
    )


def _real_keychain_mint(account: str, service: str) -> None:
    """Mint a fresh 64-hex-char secret into the login Keychain.

    Shells ``security add-generic-password -U`` exactly like
    ``keychain rotate`` so the boundary stays auditable. Raises
    ``ManifestError`` if the ``security`` binary is missing or the write
    fails.

    Security note: the minted secret is passed as the ``-w`` argv to
    ``security``.  It is therefore transiently visible via ``ps`` for the
    duration of the subprocess call — the same exposure as ``sanctum
    keychain rotate``.  The macOS ``security`` CLI has no stdin mode for
    ``add-generic-password``, so argv is the only available channel.  This
    matches the repo's secrets-opsec doctrine: short-lived argv exposure is
    accepted for minting; operators must not run ``ps`` during the call.
    """
    if not shutil.which(SECURITY_BIN):
        raise ManifestError(f"missing {SECURITY_BIN}; install Xcode Command Line Tools")
    value = _secrets.token_hex(32)  # 64 hex chars
    proc = subprocess.run(
        [SECURITY_BIN, "add-generic-password", "-a", account, "-s", service, "-w", value, "-U"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ManifestError(
            f"keychain mint failed for {service}: {proc.stderr.strip() or 'unknown error'}"
        )


def _real_remove_path(path: Path) -> bool:
    """Rename *path* to a ``.purged-<utc-stamp>`` sibling (recoverable, not a
    hard delete). Returns True iff the path existed and was renamed."""
    if not path.exists():
        return False
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    try:
        path.rename(path.with_name(f"{path.name}.purged-{stamp}"))
    except OSError as exc:
        console.print(f"  [yellow]could not remove {path}: {exc}[/]")
        return False
    return True


def _run_demo_subprocess(demo: str) -> int:
    """Run a demo command with a short timeout; return its exit code (124 on
    timeout, 127 if the command is not found)."""
    try:
        proc = subprocess.run(
            shlex.split(demo),
            capture_output=False,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124
    except FileNotFoundError:
        return 127
    return proc.returncode


# ─── CLI commands ────────────────────────────────────────────────────


@module_app.command("install", help="Install a module from a name or a manifest path.")
def install_command(
    source: Annotated[
        str,
        typer.Argument(help="Module name (e.g. backup) or path to a *.module.yaml file."),
    ],
) -> None:
    """Install a module, minting any auto-generatable secrets."""
    try:
        result = install_module(
            source,
            keychain_has=keychain.exists,
            keychain_mint=_real_keychain_mint,
        )
    except ManifestError as exc:
        console.print(f"[red]install failed:[/] {exc}")
        raise typer.Exit(2) from exc

    where = "already installed" if result.already_installed else "installed"
    console.print(f"[green]✓[/] {where}: [bold]{result.name}[/]")
    if result.minted:
        console.print(f"  minted {len(result.minted)} secret(s): {', '.join(result.minted)}")
    if result.must_supply:
        console.print("  [yellow]you must supply these operator secrets[/] (never auto-generated):")
        for svc in result.must_supply:
            console.print(f"    · {svc}  →  sanctum keychain rotate {svc} --value <your-value>")


@module_app.command("uninstall", help="Tear down a module (bootout, revoke, rename).")
def uninstall_command(
    name: Annotated[str, typer.Argument(help="Module name, e.g. backup.")],
    purge: Annotated[
        bool,
        typer.Option("--purge", help="Also remove the module's declared remove_paths."),
    ] = False,
) -> None:
    """Run the module's manifest teardown via the shared global primitives."""
    registry = ModuleRegistry.discover()
    try:
        result = uninstall_module(
            name,
            purge=purge,
            registry=registry,
            bootout_label=_bootout_full_target,
            revoke_secret=_uninstall.revoke_keychain_entry,
            rename_path=_uninstall.rename_with_suffix,
            remove_path=_real_remove_path,
        )
    except ManifestError as exc:
        console.print(str(exc))
        raise typer.Exit(2) from exc

    console.print(f"[green]✓[/] uninstalled [bold]{result.name}[/]")
    console.print(f"  booted out {len(result.bootout)} label(s)")
    console.print(f"  revoked {len(result.revoked)} secret(s)")
    if result.renamed:
        console.print(f"  renamed {len(result.renamed)} manifest file(s)")
    if purge and result.removed:
        console.print(f"  removed {len(result.removed)} path(s)")
    elif not purge and result.name:
        console.print("  [dim](remove_paths preserved — pass --purge to remove)[/]")


@module_app.command("demo", help="Run a module's demo command.")
def demo_command(
    name: Annotated[str, typer.Argument(help="Module name, e.g. backup.")],
) -> None:
    """Run the module's declared demo command and exit with its code."""
    registry = ModuleRegistry.discover()
    try:
        rc = demo_module(name, registry=registry, run_demo=_run_demo_subprocess)
    except ManifestError as exc:
        console.print(str(exc))
        raise typer.Exit(2) from exc
    raise typer.Exit(rc)
