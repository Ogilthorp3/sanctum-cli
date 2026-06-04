"""Tests for the ``sanctum module`` sub-app.

``list`` / ``status`` are read-only and exercised against the discovered
registry. ``install`` / ``uninstall`` / ``demo`` are SAFETY-SENSITIVE: every
destructive side effect (keychain mint, keychain revoke, launchd bootout,
plist/app rename, path removal) is an INJECTED callable. The tests below pass
**fake recorders** for those callables and assert against what they recorded —
NO test here touches the real macOS Keychain or the real launchd, and NO test
operates on the real ``backup`` manifest or any real ``r2-*`` / live
``com.sanctum.*`` name. The only real state used is a throwaway temp directory
pointed at by ``SANCTUM_MODULES_DIR`` (the filesystem layer is safe to exercise
for real).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import module as module_cmd
from sanctum_cli.modules.registry import ModuleRegistry

runner = CliRunner()


# ── Synthetic manifest — throwaway names ONLY ─────────────────────────
#
# `shipbar-test-key`            — fake hex64 secret, never a real cred
# `shipbar-test-supplied`       — fake operator-supplied (generate: none)
# `com.sanctum.shipbar-test-noop` — fake label, never a live service
SYNTH_MANIFEST = """\
module: testmod
version: 0.0.1
description: synthetic throwaway module for ship-bar tests
depends_on: []
services:
  - {label: com.sanctum.shipbar-test-noop, kind: launchagent, keepalive: false}
secrets:
  - {account: sanctum, service: shipbar-test-key, required: true, generate: hex64}
  - {account: sanctum, service: shipbar-test-supplied, required: true, generate: none}
probes: []
alerts: {sink: chitti, pager_conditions: []}
uninstall:
  bootout_labels: [com.sanctum.shipbar-test-noop]
  revoke_secrets: [shipbar-test-key, shipbar-test-supplied]
  remove_paths: ["~/.sanctum/shipbar-test-scratch"]
  rename_suffix: ".uninstalled-{date}"
docs: https://x.invalid/testmod
demo: "true"
soak: {min_days: 7, result_path: "~/.sanctum/soak/{module}.json"}
"""


def _write_synth(tmp_path: Path) -> Path:
    src = tmp_path / "testmod.module.yaml"
    src.write_text(SYNTH_MANIFEST, encoding="utf-8")
    return src


# ── read-only commands (existing) ────────────────────────────────────


def test_module_list_shows_backup() -> None:
    r = runner.invoke(app, ["module", "list"])
    assert r.exit_code == 0
    assert "backup" in r.stdout


def test_module_status_unknown_errors() -> None:
    r = runner.invoke(app, ["module", "status", "ghost"])
    assert r.exit_code != 0
    assert "unknown module" in r.stdout.lower() or "ghost" in r.stdout


# ── install ──────────────────────────────────────────────────────────


def test_install_copies_manifest_and_mints_hex64(tmp_path: Path) -> None:
    src = _write_synth(tmp_path)
    modules_dir = tmp_path / "modules"
    minted: list[tuple[str, str]] = []
    present: set[tuple[str, str]] = set()

    def fake_has(account: str, service: str) -> bool:
        return (account, service) in present

    def fake_mint(account: str, service: str) -> None:
        minted.append((account, service))
        present.add((account, service))

    result = module_cmd.install_module(
        str(src),
        modules_dir=modules_dir,
        keychain_has=fake_has,
        keychain_mint=fake_mint,
    )

    # manifest copied into the modules dir under its canonical name
    assert (modules_dir / "testmod.module.yaml").is_file()
    assert result.name == "testmod"
    # only the hex64 secret was minted; the `generate: none` one was NOT
    assert minted == [("sanctum", "shipbar-test-key")]
    assert "shipbar-test-key" in result.minted
    # the operator-supplied secret is surfaced as "must supply", not minted
    assert "shipbar-test-supplied" in result.must_supply


def test_install_is_idempotent(tmp_path: Path) -> None:
    src = _write_synth(tmp_path)
    modules_dir = tmp_path / "modules"
    minted: list[tuple[str, str]] = []
    present: set[tuple[str, str]] = set()

    def fake_has(account: str, service: str) -> bool:
        return (account, service) in present

    def fake_mint(account: str, service: str) -> None:
        minted.append((account, service))
        present.add((account, service))

    first = module_cmd.install_module(
        str(src), modules_dir=modules_dir, keychain_has=fake_has, keychain_mint=fake_mint
    )
    second = module_cmd.install_module(
        str(src), modules_dir=modules_dir, keychain_has=fake_has, keychain_mint=fake_mint
    )

    # secret minted exactly once across both runs
    assert minted == [("sanctum", "shipbar-test-key")]
    assert first.minted == ["shipbar-test-key"]
    # second run is a no-op for both copy and mint
    assert second.minted == []
    assert second.already_installed is True


def test_install_rejects_invalid_manifest(tmp_path: Path) -> None:
    bad = tmp_path / "bad.module.yaml"
    # revoke_secrets references an undeclared secret -> ManifestError
    bad.write_text(
        "module: testmod\nversion: 0.0.1\ndescription: bad\n"
        "docs: https://x.invalid\ndemo: 'true'\n"
        "uninstall: {revoke_secrets: [undeclared-key]}\n",
        encoding="utf-8",
    )
    modules_dir = tmp_path / "modules"

    with pytest.raises(module_cmd.ManifestError):
        module_cmd.install_module(
            str(bad),
            modules_dir=modules_dir,
            keychain_has=lambda a, s: False,
            keychain_mint=lambda a, s: None,
        )
    # nothing copied on a rejected manifest
    assert not (modules_dir / "bad.module.yaml").exists()
    assert not (modules_dir / "testmod.module.yaml").exists()


def test_install_resolves_known_module_by_name(tmp_path: Path) -> None:
    """A bare name installs from the discovered registry (built-in)."""
    modules_dir = tmp_path / "modules"
    result = module_cmd.install_module(
        "backup",  # built-in, all `generate: none` -> nothing minted
        modules_dir=modules_dir,
        keychain_has=lambda a, s: False,
        keychain_mint=lambda a, s: pytest.fail("must not mint generate:none secrets"),
    )
    assert result.name == "backup"
    assert (modules_dir / "backup.module.yaml").is_file()
    assert result.minted == []
    # the three R2 creds are operator-supplied
    assert {"r2-account-id", "r2-access-key-id", "r2-secret-access-key"} <= set(
        result.must_supply
    )


# ── uninstall ────────────────────────────────────────────────────────


def _synth_registry(tmp_path: Path) -> ModuleRegistry:
    from sanctum_cli.modules.manifest import load_manifest

    src = _write_synth(tmp_path)
    return ModuleRegistry(manifests={"testmod": load_manifest(src)})


def test_uninstall_runs_manifest_teardown_via_injected_callables(tmp_path: Path) -> None:
    reg = _synth_registry(tmp_path)
    booted: list[str] = []
    revoked: list[tuple[str, str]] = []
    renamed: list[str] = []
    removed: list[str] = []

    result = module_cmd.uninstall_module(
        "testmod",
        purge=False,
        registry=reg,
        bootout_label=lambda target: booted.append(target),
        revoke_secret=lambda account, service: (revoked.append((account, service)), True)[1],
        rename_path=lambda p, suffix: (renamed.append(str(p)), True)[1],
        remove_path=lambda p: (removed.append(str(p)), True)[1],
    )

    # the synthetic label was booted out (NOT a real com.sanctum.* service);
    # it's a launchagent so the target is gui/<uid>/<label>
    expected_target = f"gui/{os.getuid()}/com.sanctum.shipbar-test-noop"
    assert booted == [expected_target]
    # both declared revoke targets attempted, with the manifest's account
    assert revoked == [("sanctum", "shipbar-test-key"), ("sanctum", "shipbar-test-supplied")]
    # WITHOUT --purge, remove_paths is NOT touched
    assert removed == []
    assert result.bootout == ["com.sanctum.shipbar-test-noop"]
    assert result.revoked == ["shipbar-test-key", "shipbar-test-supplied"]


def test_uninstall_purge_removes_paths(tmp_path: Path) -> None:
    reg = _synth_registry(tmp_path)
    removed: list[str] = []

    module_cmd.uninstall_module(
        "testmod",
        purge=True,
        registry=reg,
        bootout_label=lambda target: None,
        revoke_secret=lambda account, service: True,
        rename_path=lambda p, suffix: True,
        remove_path=lambda p: (removed.append(str(p)), True)[1],
    )

    # under --purge the declared remove_paths ARE removed (expanded)
    assert removed == [str(Path("~/.sanctum/shipbar-test-scratch").expanduser())]


def test_uninstall_also_removes_installed_manifest(tmp_path: Path) -> None:
    """Uninstall renames the installed manifest file out of the modules dir."""
    src = _write_synth(tmp_path)
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    installed = modules_dir / "testmod.module.yaml"
    installed.write_text(src.read_text(), encoding="utf-8")

    from sanctum_cli.modules.manifest import load_manifest

    reg = ModuleRegistry(manifests={"testmod": load_manifest(installed)})
    renamed: list[str] = []

    module_cmd.uninstall_module(
        "testmod",
        purge=False,
        registry=reg,
        modules_dir=modules_dir,
        bootout_label=lambda target: None,
        revoke_secret=lambda account, service: True,
        rename_path=lambda p, suffix: (renamed.append(str(p)), True)[1],
        remove_path=lambda p: True,
    )

    assert str(installed) in renamed


def test_uninstall_unknown_module_raises(tmp_path: Path) -> None:
    reg = ModuleRegistry(manifests={})
    with pytest.raises(module_cmd.ManifestError):
        module_cmd.uninstall_module(
            "ghost",
            purge=False,
            registry=reg,
            bootout_label=lambda target: None,
            revoke_secret=lambda account, service: True,
            rename_path=lambda p, suffix: True,
            remove_path=lambda p: True,
        )


# ── demo ─────────────────────────────────────────────────────────────


def test_demo_runs_manifest_command(tmp_path: Path) -> None:
    reg = _synth_registry(tmp_path)
    ran: list[str] = []

    rc = module_cmd.demo_module(
        "testmod",
        registry=reg,
        run_demo=lambda cmd: (ran.append(cmd), 0)[1],
    )

    assert ran == ["true"]  # the synthetic manifest's demo command
    assert rc == 0


def test_demo_propagates_nonzero_exit(tmp_path: Path) -> None:
    reg = _synth_registry(tmp_path)
    rc = module_cmd.demo_module(
        "testmod",
        registry=reg,
        run_demo=lambda cmd: 7,
    )
    assert rc == 7


# ── CLI wiring (smoke via fake registry path is covered above) ────────


def test_install_cli_help_lists_subcommands() -> None:
    r = runner.invoke(app, ["module", "--help"])
    assert r.exit_code == 0
    for sub in ("install", "uninstall", "demo"):
        assert sub in r.stdout


# ── uninstall.py shared primitives (refactor target) ──────────────────


def test_uninstall_primitives_are_importable() -> None:
    """The shared teardown primitives live in uninstall.py and are importable
    so module.py can reuse them rather than duplicating bootout/revoke/rename."""
    from sanctum_cli.commands import uninstall as un

    assert callable(un.bootout_label)
    assert callable(un.revoke_keychain_entry)
    assert callable(un.rename_with_suffix)


# ── bootout domain routing ─────────────────────────────────────────────
#
# The bootout callable receives the full launchctl target string, not just
# the label. launchdaemon services resolve to "system/<label>"; launchagent
# (and any other kind) resolve to "gui/<uid>/<label>".

_DAEMON_MANIFEST = """\
module: testdaemon
version: 0.0.1
description: synthetic daemon module for domain-routing tests
services:
  - label: com.sanctum.shipbar-test-daemon
    kind: launchdaemon
    keepalive: false
uninstall:
  bootout_labels: [com.sanctum.shipbar-test-daemon]
  revoke_secrets: []
  remove_paths: []
  rename_suffix: ".uninstalled-{date}"
docs: https://x.invalid/testdaemon
demo: "true"
"""

_AGENT_MANIFEST = """\
module: testagent
version: 0.0.1
description: synthetic agent module for domain-routing tests
services:
  - label: com.sanctum.shipbar-test-agent
    kind: launchagent
    keepalive: false
uninstall:
  bootout_labels: [com.sanctum.shipbar-test-agent]
  revoke_secrets: []
  remove_paths: []
  rename_suffix: ".uninstalled-{date}"
docs: https://x.invalid/testagent
demo: "true"
"""


def _reg_from_yaml(tmp_path: Path, yaml_text: str) -> ModuleRegistry:
    from sanctum_cli.modules.manifest import load_manifest

    src = tmp_path / "mod.module.yaml"
    src.write_text(yaml_text, encoding="utf-8")
    m = load_manifest(src)
    return ModuleRegistry(manifests={m.module: m})


def test_bootout_uses_system_domain_for_launchdaemon(tmp_path: Path) -> None:
    """A launchdaemon service → bootout target is system/<label>."""
    reg = _reg_from_yaml(tmp_path, _DAEMON_MANIFEST)
    booted: list[str] = []

    module_cmd.uninstall_module(
        "testdaemon",
        purge=False,
        registry=reg,
        bootout_label=lambda target: booted.append(target),
        revoke_secret=lambda account, service: True,
        rename_path=lambda p, suffix: True,
        remove_path=lambda p: True,
    )

    assert booted == ["system/com.sanctum.shipbar-test-daemon"]


def test_bootout_uses_gui_domain_for_launchagent(tmp_path: Path) -> None:
    """A launchagent service → bootout target is gui/<uid>/<label>."""
    reg = _reg_from_yaml(tmp_path, _AGENT_MANIFEST)
    booted: list[str] = []

    module_cmd.uninstall_module(
        "testagent",
        purge=False,
        registry=reg,
        bootout_label=lambda target: booted.append(target),
        revoke_secret=lambda account, service: True,
        rename_path=lambda p, suffix: True,
        remove_path=lambda p: True,
    )

    assert booted == [f"gui/{os.getuid()}/com.sanctum.shipbar-test-agent"]
