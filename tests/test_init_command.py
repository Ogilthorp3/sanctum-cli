"""``sanctum init`` — first-run config bootstrap command.

The v0.9.0 onboarding blocker: a fresh machine had no way to create
``~/.sanctum/instance.yaml`` short of hand-writing YAML, so every primary
command died exit 5 (ConfigError) on first run. ``sanctum init`` writes the
smallest file the strict loader accepts and is idempotent by default.

On a machine provisioned before ``init`` existed (a second haus with only a
``router:`` block), ``init`` adds or updates the ``instance:`` block in place;
the editor itself is covered by ``test_config_instance_edit.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from sanctum_cli import config
from sanctum_cli.cli import app
from sanctum_cli.commands import init_cmd
from sanctum_cli.errors import ExitCode

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions"
)

ROUTER_ONLY = (
    "# second haus\n"
    "router:\n"
    "  # local brain\n"
    "  default: local\n"
    "  backends:\n"
    "    local:\n"
    "      url: http://127.0.0.1:1338/v1\n"
)
HUB_BLOCK = "instance:\n  name: Old Haus\n  slug: old-haus\n  role: hub\n"


def _instance_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | bytes) -> Path:
    target = tmp_path / "instance.yaml"
    target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))
    return target


def test_init_creates_loadable_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh ``init --yes`` writes a file the strict loader accepts."""
    target = tmp_path / "instance.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))
    assert not target.exists()

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert target.exists()
    cfg = config.load(target)  # would raise ConfigError if invalid
    assert cfg.instance.name
    assert cfg.instance.slug


def test_init_honors_env_instance_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """init writes to ``$SANCTUM_INSTANCE_FILE``, not the default location."""
    target = tmp_path / "nested" / "custom.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert target.exists(), "init must honor SANCTUM_INSTANCE_FILE"


def test_init_is_idempotent_does_not_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running init leaves an existing file untouched and exits 0."""
    target = tmp_path / "instance.yaml"
    target.write_text("instance:\n  name: Existing Haus\n  slug: existing-haus\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "already exists" in (result.stdout + (result.stderr or "")).lower()
    assert config.load(target).instance.name == "Existing Haus"


def test_init_force_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--force replaces an existing file with a freshly named one."""
    target = tmp_path / "instance.yaml"
    target.write_text("instance:\n  name: Old\n  slug: old\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--name", "New Haus", "--force"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cfg = config.load(target)
    assert cfg.instance.name == "New Haus"
    assert cfg.instance.slug == "new-haus"


def test_init_name_derives_safe_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A messy --name still yields a lowercase, bucket-safe slug."""
    target = tmp_path / "instance.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--name", "Alex's Haus!! 2026"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cfg = config.load(target)
    assert cfg.instance.slug == cfg.instance.slug.lower()
    assert all(c.isalnum() or c == "-" for c in cfg.instance.slug)
    assert cfg.instance.slug, "slug must not be empty even from a punctuation-heavy name"


def test_init_role_satellite_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--role satellite lands in instance.role and validates."""
    target = tmp_path / "instance.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--name", "Second Haus", "--role", "satellite"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert config.load(target).instance.role == "satellite"


def test_init_rejects_unknown_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "instance.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--yes", "--role", "castle"])

    assert result.exit_code != 0
    assert not target.exists()


def test_init_adds_missing_instance_block_and_keeps_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-init file with only other blocks gets instance: added, nothing lost."""
    target = tmp_path / "instance.yaml"
    original = (
        "router:\n"
        "  # local brain\n"
        "  default: local\n"
        "  backends:\n"
        "    local:\n"
        "      url: http://127.0.0.1:1338/v1\n"
    )
    target.write_text(original, encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--name", "Second Haus", "--role", "satellite"])

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    cfg = config.load(target)
    assert cfg.instance.name == "Second Haus"
    assert cfg.instance.role == "satellite"
    text = target.read_text(encoding="utf-8")
    assert text.endswith(original), "existing blocks and comments must survive verbatim"
    assert config.instance_value("router.backends.local.url", path=target) == (
        "http://127.0.0.1:1338/v1"
    )
    assert config.backup_path(target).read_text(encoding="utf-8") == original


def test_init_reports_unparseable_pre_init_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "instance.yaml"
    target.write_text("router: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 1
    assert target.read_text(encoding="utf-8") == "router: [unclosed\n"


# ─── a file without an instance: block ──────────────────────────────


def test_init_add_path_prompts_for_the_name_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _instance_file(tmp_path, monkeypatch, ROUTER_ONLY)

    result = runner.invoke(app, ["init"], input="Prompted Haus\n")

    assert result.exit_code == 0, result.output
    assert "instance name" in result.output
    cfg = config.load(target)
    assert (cfg.instance.name, cfg.instance.slug) == ("Prompted Haus", "prompted-haus")
    assert target.read_text(encoding="utf-8").endswith(ROUTER_ONLY)


def test_init_add_path_takes_the_hostname_default_under_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _instance_file(tmp_path, monkeypatch, ROUTER_ONLY)
    monkeypatch.setattr(config, "_default_identity", lambda: ("Box Sanctum", "box"))

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 0, result.output
    cfg = config.load(target)
    assert (cfg.instance.name, cfg.instance.slug) == ("Box Sanctum", "box-sanctum")


def test_init_without_a_terminal_asks_for_flags_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _instance_file(tmp_path, monkeypatch, ROUTER_ONLY)

    result = runner.invoke(app, ["init"])  # stdin is empty: the prompt hits EOF

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "--name" in result.output
    assert target.read_text(encoding="utf-8") == ROUTER_ONLY


def test_init_fills_a_template_whose_instance_keys_are_commented_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = "instance:\n  # name: My Haus\n  # slug: my-haus\nrouter:\n  default: local\n"
    target = _instance_file(tmp_path, monkeypatch, template)

    result = runner.invoke(app, ["init", "--name", "Second Haus", "--role", "satellite"])

    assert result.exit_code == 0, result.output
    assert config.load(target).instance.role == "satellite"
    text = target.read_text(encoding="utf-8")
    assert text.startswith("instance:\n  name: Second Haus\n")
    assert "  # name: My Haus\n" in text


@pytest.mark.parametrize("content", ["", ROUTER_ONLY])
def test_init_name_with_rich_markup_is_printed_literally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    target = tmp_path / "instance.yaml"
    if content:
        target.write_text(content, encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--name", "Haus [/] :house:"])

    assert result.exit_code == 0, result.output
    assert "Haus [/] :house:" in result.output
    assert config.load(target).instance.name == "Haus [/] :house:"


def test_init_refuses_a_flow_style_instance_and_prints_the_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "instance: {name: Old, slug: old}\nrouter: 1\n"
    target = _instance_file(tmp_path, monkeypatch, content)

    result = runner.invoke(app, ["init", "--role", "satellite"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "cannot safely edit" in result.output
    assert "    role: satellite" in result.output
    assert target.read_text(encoding="utf-8") == content


def test_init_scalar_instance_is_refused_with_the_whole_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = "instance: my haus\n"
    target = _instance_file(tmp_path, monkeypatch, content)

    result = runner.invoke(app, ["init", "--name", "Second Haus"])

    assert result.exit_code == 1
    assert "    instance:\n      name: Second Haus\n      slug: second-haus" in result.output
    assert target.read_text(encoding="utf-8") == content


def test_init_non_utf8_file_exits_1_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"router:\n  label: caf\xe9\n"
    target = _instance_file(tmp_path, monkeypatch, content)

    result = runner.invoke(app, ["init", "--yes"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "UTF-8" in result.output
    assert target.read_bytes() == content


@needs_non_root
def test_init_unreadable_file_exits_1_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _instance_file(tmp_path, monkeypatch, ROUTER_ONLY)
    target.chmod(0)
    try:
        result = runner.invoke(app, ["init", "--yes"])
    finally:
        target.chmod(0o600)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "cannot read" in result.output
    assert target.read_text(encoding="utf-8") == ROUTER_ONLY


def test_init_markup_in_the_path_is_printed_literally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "cfg [red]"
    folder.mkdir()
    target = folder / "instance.yaml"
    target.write_text("instance: {name: Old, slug: old}\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--role", "hub"])

    assert result.exit_code == 1
    assert f"cannot safely edit {target}" in result.output


def test_init_reports_a_file_that_still_fails_to_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block is added, but a broken cli: block still fails the strict loader."""
    target = _instance_file(tmp_path, monkeypatch, "cli:\n  default_provider: nope\n")

    result = runner.invoke(app, ["init", "--name", "Second Haus"])

    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "does not load yet" in result.output
    assert "default_provider" in result.output
    assert config.instance_value("instance.slug", path=target) == "second-haus"


# ─── a file with an instance: block ─────────────────────────────────


def test_init_role_updates_an_existing_block_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = HUB_BLOCK + ROUTER_ONLY
    target = _instance_file(tmp_path, monkeypatch, original)

    result = runner.invoke(app, ["init", "--role", "satellite"])

    assert result.exit_code == 0, result.output
    assert "role: hub -> satellite" in result.output
    assert target.read_text(encoding="utf-8") == original.replace("role: hub", "role: satellite")
    assert config.backup_path(target).read_text(encoding="utf-8") == original


def test_init_name_updates_an_existing_block_and_keeps_the_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = HUB_BLOCK.replace("Old Haus", "Old [/] Haus") + ROUTER_ONLY
    target = _instance_file(tmp_path, monkeypatch, original)

    result = runner.invoke(app, ["init", "--name", "New Haus"])

    assert result.exit_code == 0, result.output
    assert "name: Old [/] Haus -> New Haus" in result.output
    assert "slug stays old-haus" in result.output
    cfg = config.load(target)
    assert (cfg.instance.name, cfg.instance.slug, cfg.instance.role) == (
        "New Haus",
        "old-haus",
        "hub",
    )


def test_init_with_values_already_set_is_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = HUB_BLOCK + ROUTER_ONLY
    target = _instance_file(tmp_path, monkeypatch, original)

    result = runner.invoke(app, ["init", "--role", "hub", "--name", "Old Haus"])

    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output
    assert target.read_text(encoding="utf-8") == original
    assert not config.backup_path(target).exists()


def test_init_without_flags_does_not_offer_force_over_other_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = HUB_BLOCK + ROUTER_ONLY
    target = _instance_file(tmp_path, monkeypatch, original)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "already exists" in result.output
    assert "re-run with --force" not in result.output
    assert "drop its other blocks (router)" in result.output
    assert "by hand" in result.output
    assert target.read_text(encoding="utf-8") == original


def test_init_without_flags_offers_force_for_an_instance_only_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _instance_file(tmp_path, monkeypatch, HUB_BLOCK)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "re-run with --force to overwrite it" in result.output


def test_init_force_keeps_a_backup_of_the_replaced_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = HUB_BLOCK + ROUTER_ONLY
    target = _instance_file(tmp_path, monkeypatch, original)

    result = runner.invoke(app, ["init", "--force", "--name", "Fresh Haus"])

    assert result.exit_code == 0, result.output
    assert "overwrote" in result.output
    assert config.backup_path(target).read_text(encoding="utf-8") == original
    assert config.load(target).instance.name == "Fresh Haus"


def test_init_force_on_a_new_file_says_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "instance.yaml"
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--force", "--yes"])

    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert "overwrote" not in result.output
    assert not config.backup_path(target).exists()


# ─── nothing to change: the file must still load ────────────────────


def test_up_to_date_values_on_a_file_that_does_not_load_exit_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'already up to date' is only true of a file the strict loader accepts."""
    content = "instance:\n  name: Old Haus\n  slug: old-haus\n  role: Hub\n"  # not a role
    target = _instance_file(tmp_path, monkeypatch, content)

    result = runner.invoke(app, ["init", "--name", "Old Haus"])

    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert "already up to date" not in result.output
    assert f"{target} does not load:" in result.output
    assert "instance.role" in result.output
    assert target.read_text(encoding="utf-8") == content
    assert not config.backup_path(target).exists()


def test_no_flags_on_a_file_that_does_not_load_exit_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = HUB_BLOCK + "cli:\n  default_provider: nope\n"
    target = _instance_file(tmp_path, monkeypatch, content)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert "already exists" not in result.output
    assert "default_provider" in result.output
    assert target.read_text(encoding="utf-8") == content


# ─── a block without a slug gets one from its name ──────────────────

NO_SLUG = "instance:\n  name: Old Haus\n  role: hub\n"


def test_name_on_a_block_without_a_slug_sets_the_slug_from_the_new_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _instance_file(tmp_path, monkeypatch, NO_SLUG + ROUTER_ONLY)

    result = runner.invoke(app, ["init", "--name", "New Haus"])

    assert result.exit_code == 0, result.output
    assert "  name: Old Haus -> New Haus" in result.output
    assert "  slug: (unset) -> new-haus" in result.output
    assert "slug stays" not in result.output
    cfg = config.load(target)
    assert (cfg.instance.name, cfg.instance.slug, cfg.instance.role) == (
        "New Haus",
        "new-haus",
        "hub",
    )
    assert target.read_text(encoding="utf-8").endswith(ROUTER_ONLY)


@pytest.mark.parametrize("block", [NO_SLUG, NO_SLUG + "  slug:\n"])
def test_role_on_a_block_without_a_slug_derives_it_from_the_existing_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: str
) -> None:
    target = _instance_file(tmp_path, monkeypatch, block)

    result = runner.invoke(app, ["init", "--role", "satellite"])

    assert result.exit_code == 0, result.output
    assert "  slug: (unset) -> old-haus" in result.output
    assert "  role: hub -> satellite" in result.output
    cfg = config.load(target)
    assert (cfg.instance.slug, cfg.instance.role) == ("old-haus", "satellite")


def test_no_flags_completes_a_missing_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _instance_file(tmp_path, monkeypatch, NO_SLUG)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "  slug: (unset) -> old-haus" in result.output
    assert config.load(target).instance.slug == "old-haus"
    assert config.backup_path(target).read_text(encoding="utf-8") == NO_SLUG


# ─── an instance: the editor will not rewrite is refused up front ───


@pytest.mark.parametrize("content", ["instance: my haus\n", "instance:\n  - my haus\n"])
def test_unusable_instance_is_refused_before_asking_for_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    target = _instance_file(tmp_path, monkeypatch, content)

    def no_prompt(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("init prompted for a name it cannot use")

    monkeypatch.setattr(init_cmd.Prompt, "ask", no_prompt)

    result = runner.invoke(app, ["init", "--role", "hub"], input="Typed Haus\n")

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert (
        "    instance:\n      name: <instance name>\n      slug: <instance-slug>\n      role: hub"
    ) in result.output
    assert target.read_text(encoding="utf-8") == content


# ─── values are printed as text, never as Rich markup ───────────────


@pytest.mark.parametrize("content", ["", ROUTER_ONLY])
@pytest.mark.parametrize("name", ["Haus\\\\", "Haus\\\\\\", "[bold]Haus\\"])
def test_a_name_ending_in_backslashes_is_printed_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, name: str
) -> None:
    """Rich's escape() leaves two or more trailing backslashes, which then eat a tag."""
    target = tmp_path / "instance.yaml"
    if content:
        target.write_text(content, encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))

    result = runner.invoke(app, ["init", "--name", name])

    assert result.exit_code == 0, result.output
    assert f"  instance: {name} (slug={config.slugify_name(name)})" in result.output
    assert config.load(target).instance.name == name


def test_a_rename_to_backslashes_prints_old_and_new_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _instance_file(tmp_path, monkeypatch, HUB_BLOCK.replace("Old Haus", "Old [/] Haus\\\\"))

    result = runner.invoke(app, ["init", "--name", "New\\\\\\"])

    assert result.exit_code == 0, result.output
    assert "  name: Old [/] Haus\\\\ -> New\\\\\\" in result.output


# ─── help text ──────────────────────────────────────────────────────


def test_init_help_describes_the_in_place_edit() -> None:
    result = runner.invoke(app, ["init", "--help"], env={"COLUMNS": "240", "TERMINAL_WIDTH": "240"})

    assert result.exit_code == 0
    text = " ".join(result.output.replace("│", " ").split())
    for phrase in (
        "Create ~/.sanctum/instance.yaml, or add/update its instance: block (name, role) in place.",
        "Instance name (skips the prompt). Renames an existing instance: block; its slug stays.",
        "Node role: satellite (contained remote site) or hub. Updates an existing instance: "
        "block in place.",
        "Replace the whole instance.yaml, dropping its other blocks (the old file is saved to "
        "instance.yaml.bak).",
    ):
        assert phrase in text


# ─── a real process: nothing may crash at import time ───────────────


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param(
            "instance:\n  name: Café\n  slug: cafe\n".encode("latin-1"),
            "{target} is not UTF-8 text (undecodable byte at offset 21)",
            id="latin-1",
        ),
        pytest.param(
            b"instance:\n  name: Box\n  slug: box\nsince: 2026-02-30\n",
            "cannot read {target}: 2026-02-30 on line 4 is not a valid date "
            "(day is out of range for month)",
            id="impossible-date",
        ),
        pytest.param(
            b"instance:\n  name: Box\n  slug: box\nflag: !!bool maybe\n",
            "cannot read {target}: !!bool maybe on line 4 is not a valid true/false value",
            id="bool-maybe",
        ),
        pytest.param(
            b'instance:\n  name: Box\n  slug: box\nport: !!int ""\n',
            'cannot read {target}: !!int "" on line 4 is not a valid whole number',
            id="empty-int",
        ),
    ],
)
def test_init_in_a_real_process_fails_with_one_clean_line(
    tmp_path: Path, content: bytes, message: str
) -> None:
    """CliRunner imports the CLI once, up front; only a child process sees import-time
    reads of instance.yaml (council's seats call instance_value while importing)."""
    target = tmp_path / "instance.yaml"
    target.write_bytes(content)
    home = tmp_path / "home"  # conftest's fake home: the child must not read the real one
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "SANCTUM_INSTANCE_FILE": str(target),
        "HOME": str(home),
        "PYTHONIOENCODING": "utf-8",
    }

    proc = subprocess.run(
        [sys.executable, "-m", "sanctum_cli", "init", "--yes"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )

    assert proc.returncode != 0
    assert "Traceback" not in proc.stdout + proc.stderr
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert lines[0] == "✗ " + message.format(target=target), proc.stderr
    assert all(line.startswith("fix: ") for line in lines[1:]), proc.stderr
    assert len(lines) == 2, proc.stderr
    assert target.read_bytes() == content
