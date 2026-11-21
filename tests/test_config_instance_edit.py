"""``config.set_instance_fields`` — the one safe editor for an existing instance.yaml.

``sanctum init`` must add or update the ``instance:`` block of a file that also
holds other blocks and the operator's comments. These tests pin the contract:
everything outside the edited lines survives byte for byte (BOM, CRLF/CR line
endings, comments, a missing trailing newline), values are quoted so any name
reads back exactly, the result is re-parsed before anything is written, and a
file the editor cannot edit safely raises ``ConfigError`` and stays untouched.
"""

from __future__ import annotations

import os
import signal
import stat
import tempfile
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from sanctum_cli import config
from sanctum_cli.errors import ConfigError

NEW = {"name": "Second Haus", "slug": "second-haus", "role": "satellite"}
NEW_LINES = "instance:\n  name: Second Haus\n  slug: second-haus\n  role: satellite\n"

needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions"
)


def _write(tmp_path: Path, content: bytes | str, name: str = "instance.yaml") -> Path:
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


def _refused(path: Path, before: bytes, **fields: str) -> ConfigError:
    """Assert the edit raises ConfigError and leaves the file (and no .bak) behind."""
    with pytest.raises(ConfigError) as caught:
        config.set_instance_fields(path, **fields)
    assert path.read_bytes() == before, "a refused edit must not touch the file"
    assert not config.backup_path(path).exists()
    return caught.value


# ─── no instance key: a whole block is inserted ─────────────────────


def test_block_goes_at_the_top_and_the_rest_survives(tmp_path: Path) -> None:
    original = "router:\n  # local brain\n  default: local\n"
    path = _write(tmp_path, original)

    changes = config.set_instance_fields(path, **NEW)

    assert path.read_text(encoding="utf-8") == NEW_LINES + original
    assert changes == {key: (None, value) for key, value in NEW.items()}
    assert config.load(path).instance.role == "satellite"


def test_block_goes_after_a_leading_document_marker(tmp_path: Path) -> None:
    path = _write(tmp_path, "---\nrouter:\n  a: 1\n")

    config.set_instance_fields(path, **NEW)

    assert path.read_text(encoding="utf-8") == "---\n" + NEW_LINES + "router:\n  a: 1\n"


def test_block_goes_after_yaml_directive_and_marker(tmp_path: Path) -> None:
    path = _write(tmp_path, "# managed\n%YAML 1.1\n---\nrouter: 1\n")

    config.set_instance_fields(path, **NEW)

    expected = "# managed\n%YAML 1.1\n---\n" + NEW_LINES + "router: 1\n"
    assert path.read_text(encoding="utf-8") == expected
    assert config.load(path).instance.name == "Second Haus"


def test_utf8_bom_is_kept(tmp_path: Path) -> None:
    bom = b"\xef\xbb\xbf"
    path = _write(tmp_path, bom + b"router: 1\n")

    config.set_instance_fields(path, **NEW)

    assert path.read_bytes() == bom + NEW_LINES.encode("utf-8") + b"router: 1\n"
    assert config.load(path).instance.slug == "second-haus"


def test_crlf_line_endings_are_kept_and_reused(tmp_path: Path) -> None:
    original = b"router:\r\n  # local brain\r\n  default: local\r\n"
    path = _write(tmp_path, original)

    config.set_instance_fields(path, **NEW)

    data = path.read_bytes()
    assert data == NEW_LINES.replace("\n", "\r\n").encode("utf-8") + original
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert config.load(path).instance.name == "Second Haus"


def test_cr_only_line_endings_are_kept_and_reused(tmp_path: Path) -> None:
    original = b"router:\r  default: local\r"
    path = _write(tmp_path, original)

    config.set_instance_fields(path, **NEW)

    data = path.read_bytes()
    assert data == NEW_LINES.replace("\n", "\r").encode("utf-8") + original
    assert b"\n" not in data
    assert config.load(path).instance.role == "satellite"


def test_missing_trailing_newline_is_not_added(tmp_path: Path) -> None:
    path = _write(tmp_path, "router: 1")

    config.set_instance_fields(path, **NEW)

    assert path.read_text(encoding="utf-8") == NEW_LINES + "router: 1"


def test_empty_file_gets_just_the_block(tmp_path: Path) -> None:
    path = _write(tmp_path, b"")

    config.set_instance_fields(path, **NEW)

    assert path.read_text(encoding="utf-8") == NEW_LINES


def test_comments_only_file_keeps_its_comments(tmp_path: Path) -> None:
    path = _write(tmp_path, "# filled in by sanctum init\n# see the docs\n")

    config.set_instance_fields(path, **NEW)

    expected = NEW_LINES + "# filled in by sanctum init\n# see the docs\n"
    assert path.read_text(encoding="utf-8") == expected


def test_indented_top_level_gets_a_matching_block(tmp_path: Path) -> None:
    path = _write(tmp_path, "  router: 1\n")

    config.set_instance_fields(path, name="Second Haus", slug="second-haus")

    expected = "  instance:\n    name: Second Haus\n    slug: second-haus\n  router: 1\n"
    assert path.read_text(encoding="utf-8") == expected


# ─── instance: present but empty / null ─────────────────────────────


def test_null_instance_with_commented_children_is_filled_in_place(tmp_path: Path) -> None:
    original = "instance:\n#    name: My Haus\n#    slug: my-haus\nrouter:\n    default: local\n"
    path = _write(tmp_path, original)

    config.set_instance_fields(path, **NEW)

    # Keys land right under ``instance:`` at the file's own 4-space step.
    expected = (
        "instance:\n"
        "    name: Second Haus\n"
        "    slug: second-haus\n"
        "    role: satellite\n"
        "#    name: My Haus\n"
        "#    slug: my-haus\n"
        "router:\n"
        "    default: local\n"
    )
    assert path.read_text(encoding="utf-8") == expected
    assert config.load(path).instance.name == "Second Haus"


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        ("instance: ~  # fill me\n", "instance:  # fill me\n"),
        ("instance: null\n", "instance:\n"),
        ("instance: {}\n", "instance:\n"),
    ],
)
def test_explicit_null_or_empty_instance_is_filled(tmp_path: Path, line: str, kept: str) -> None:
    path = _write(tmp_path, line + "router: 1\n")

    config.set_instance_fields(path, name="Second Haus", slug="second-haus")

    expected = kept + "  name: Second Haus\n  slug: second-haus\nrouter: 1\n"
    assert path.read_text(encoding="utf-8") == expected


def test_null_instance_on_the_last_line_without_newline(tmp_path: Path) -> None:
    path = _write(tmp_path, "router: 1\ninstance:")

    config.set_instance_fields(path, name="Second Haus", slug="second-haus")

    expected = "router: 1\ninstance:\n  name: Second Haus\n  slug: second-haus"
    assert path.read_text(encoding="utf-8") == expected
    assert config.load(path).instance.slug == "second-haus"


# ─── instance: block mapping: update in place ───────────────────────

EXISTING = (
    "# second haus config\n"
    "instance:\n"
    "  name: Old Haus  # display only\n"
    "  slug: old-haus\n"
    "  role: hub  # was the primary\n"
    "router:\n"
    "  # local brain first\n"
    "  default: local\n"
    "  backends:\n"
    "    local:\n"
    "      url: http://127.0.0.1:1338/v1\n"
)


def test_role_update_keeps_router_and_comments_byte_for_byte(tmp_path: Path) -> None:
    path = _write(tmp_path, EXISTING)

    changes = config.set_instance_fields(path, role="satellite")

    assert changes == {"role": ("hub", "satellite")}
    expected = EXISTING.replace("role: hub  #", "role: satellite  #")
    assert path.read_text(encoding="utf-8") == expected


def test_name_update_keeps_the_slug(tmp_path: Path) -> None:
    path = _write(tmp_path, EXISTING)

    changes = config.set_instance_fields(path, name="New Haus")

    assert changes == {"name": ("Old Haus", "New Haus")}
    expected = EXISTING.replace("name: Old Haus  #", "name: New Haus  #")
    assert path.read_text(encoding="utf-8") == expected
    assert config.load(path).instance.slug == "old-haus"


def test_slug_is_never_changed_on_an_existing_block(tmp_path: Path) -> None:
    path = _write(tmp_path, EXISTING)

    error = _refused(path, EXISTING.encode("utf-8"), slug="new-haus")

    assert "slug" in error.message


def test_missing_role_is_inserted_under_instance_at_block_indent(tmp_path: Path) -> None:
    original = "instance:  # identity\n    name: Old Haus\n    slug: old-haus\nrouter: 1\n"
    path = _write(tmp_path, original)

    config.set_instance_fields(path, role="satellite")

    expected = (
        "instance:  # identity\n"
        "    role: satellite\n"
        "    name: Old Haus\n"
        "    slug: old-haus\n"
        "router: 1\n"
    )
    assert path.read_text(encoding="utf-8") == expected


def test_empty_role_value_is_filled_and_its_comment_kept(tmp_path: Path) -> None:
    path = _write(tmp_path, "instance:\n  name: A\n  slug: a\n  role:   # set me\n")

    config.set_instance_fields(path, role="satellite")

    expected = "instance:\n  name: A\n  slug: a\n  role: satellite   # set me\n"
    assert path.read_text(encoding="utf-8") == expected


def test_update_keeps_crlf(tmp_path: Path) -> None:
    original = "instance:\r\n  name: A\r\n  slug: a\r\nrouter: 1\r\n"
    path = _write(tmp_path, original)

    config.set_instance_fields(path, name="B", role="hub")

    expected = "instance:\r\n  role: hub\r\n  name: B\r\n  slug: a\r\nrouter: 1\r\n"
    assert path.read_bytes() == expected.encode("utf-8")


def test_no_change_writes_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path, EXISTING)
    before = path.stat().st_mtime_ns

    assert config.set_instance_fields(path, name="Old Haus", role="hub") == {}

    assert path.read_text(encoding="utf-8") == EXISTING
    assert path.stat().st_mtime_ns == before
    assert not config.backup_path(path).exists()


# ─── refusals: ConfigError, file untouched, lines to add by hand ────


def test_scalar_instance_is_refused_with_the_exact_lines(tmp_path: Path) -> None:
    before = b"instance: my haus\nrouter: 1\n"
    path = _write(tmp_path, before)

    error = _refused(path, before, **NEW)

    assert "single value" in error.message
    assert error.fix is not None
    assert "    instance:\n      name: Second Haus\n      slug: second-haus" in error.fix
    assert "      role: satellite" in error.fix


def test_flow_mapping_instance_is_refused_with_the_exact_lines(tmp_path: Path) -> None:
    before = b"instance: {name: Old, slug: old}\nrouter: 1\n"
    path = _write(tmp_path, before)

    error = _refused(path, before, role="satellite")

    assert error.fix is not None
    assert error.fix.endswith("\n    role: satellite")


def test_list_instance_is_refused(tmp_path: Path) -> None:
    before = b"instance:\n  - name\nrouter: 1\n"
    path = _write(tmp_path, before)

    error = _refused(path, before, **NEW)

    assert "list" in error.message


def test_list_file_is_refused(tmp_path: Path) -> None:
    before = b"- router\n- cli\n"
    path = _write(tmp_path, before)

    error = _refused(path, before, **NEW)

    assert "mapping" in error.message


def test_invalid_yaml_is_refused(tmp_path: Path) -> None:
    before = b"router: [unclosed\n"
    path = _write(tmp_path, before)

    error = _refused(path, before, **NEW)

    assert "YAML parse error" in error.message


def test_non_utf8_file_is_a_config_error_not_a_traceback(tmp_path: Path) -> None:
    before = b"router:\n  label: caf\xe9\n"
    path = _write(tmp_path, before)

    error = _refused(path, before, **NEW)

    assert "UTF-8" in error.message
    assert error.fix


@needs_non_root
def test_unreadable_file_is_a_config_error_not_a_traceback(tmp_path: Path) -> None:
    path = _write(tmp_path, "router: 1\n")
    path.chmod(0)
    try:
        with pytest.raises(ConfigError) as caught:
            config.set_instance_fields(path, **NEW)
    finally:
        path.chmod(0o600)
    assert "cannot read" in caught.value.message
    assert path.read_text(encoding="utf-8") == "router: 1\n"


@needs_non_root
def test_read_only_file_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, "router: 1\n")
    path.chmod(0o444)
    try:
        with pytest.raises(ConfigError) as caught:
            config.set_instance_fields(path, **NEW)
    finally:
        path.chmod(0o600)
    assert "read-only" in caught.value.message
    assert path.read_text(encoding="utf-8") == "router: 1\n"
    assert not config.backup_path(path).exists()


def test_duplicate_instance_keys_are_refused(tmp_path: Path) -> None:
    before = b"instance:\n  name: A\n  slug: a\ninstance:\n  name: B\n  slug: b\n"
    path = _write(tmp_path, before)

    error = _refused(path, before, role="hub")

    assert "more than one instance" in error.message


def test_anchored_value_is_refused(tmp_path: Path) -> None:
    before = b"instance:\n  name: &n Old\n  slug: old\nlabel: *n\n"
    path = _write(tmp_path, before)

    _refused(path, before, name="New")


def test_content_on_the_document_marker_line_is_refused(tmp_path: Path) -> None:
    before = b"--- !!map\nrouter: 1\n"
    path = _write(tmp_path, before)

    _refused(path, before, **NEW)


def test_edit_that_does_not_read_back_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-parse guard: an unquoted ``yes`` would load as True, so nothing is written."""
    before = b"router: 1\n"
    path = _write(tmp_path, before)
    monkeypatch.setattr(config, "_yaml_scalar", lambda value: value)

    error = _refused(path, before, name="yes", slug="yes")

    assert "did not read back" in error.message


def test_unknown_role_is_rejected(tmp_path: Path) -> None:
    before = b"router: 1\n"
    path = _write(tmp_path, before)

    _refused(path, before, name="A", slug="a", role="castle")


def test_new_block_needs_name_and_slug(tmp_path: Path) -> None:
    path = _write(tmp_path, "router: 1\n")

    with pytest.raises(ValueError, match="name and a slug"):
        config.set_instance_fields(path, role="satellite")


# ─── quoting: any name reads back exactly ───────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "Haus: 2026",
        "Haus # one",
        "*star",
        "&anchor",
        "yes",
        "no",
        "on",
        "off",
        "123",
        "1.5",
        "null",
        "~",
        "  padded  ",
        "Haus [/]",
        "'quoted'",
        '"double"',
        "- dash",
        "line\nbreak",
        "tab\there",
        "nel\x85sep",
        "ls\u2028ps\u2029",
        "Maison Ünïcode ☃ 🏠",
    ],
)
def test_names_round_trip_exactly(tmp_path: Path, name: str) -> None:
    added = _write(tmp_path, "router: 1\n", "added.yaml")
    config.set_instance_fields(added, name=name, slug="haus")
    assert config.load(added).instance.name == name

    renamed = _write(tmp_path, EXISTING, "renamed.yaml")
    config.set_instance_fields(renamed, name=name)
    assert config.load(renamed).instance.name == name
    assert config.instance_value("router.default", path=renamed) == "local"


def test_unicode_name_is_stored_as_readable_utf8(tmp_path: Path) -> None:
    path = _write(tmp_path, "router: 1\n")

    config.set_instance_fields(path, name="Maison Ünïcode ☃", slug="maison")

    assert "name: Maison Ünïcode ☃\n" in path.read_text(encoding="utf-8")


@settings(max_examples=150, deadline=None)
@given(name=st.text(max_size=40))
def test_any_text_name_round_trips(name: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "instance.yaml"
        path.write_text(EXISTING, encoding="utf-8")
        config.set_instance_fields(path, name=name)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["instance"]["name"] == name
        assert data["instance"]["slug"] == "old-haus"
        assert data["router"]["backends"]["local"]["url"] == "http://127.0.0.1:1338/v1"


# ─── writing: backup, mode, symlink ─────────────────────────────────


def test_backup_holds_the_original_bytes_and_mode_is_kept(tmp_path: Path) -> None:
    path = _write(tmp_path, EXISTING)
    path.chmod(0o640)

    config.set_instance_fields(path, role="satellite")

    backup = config.backup_path(path)
    assert backup == tmp_path / "instance.yaml.bak"
    assert backup.read_text(encoding="utf-8") == EXISTING
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_symlinked_file_is_edited_through_the_link(tmp_path: Path) -> None:
    real = _write(tmp_path, EXISTING, "real.yaml")
    link = tmp_path / "instance.yaml"
    link.symlink_to(real)

    config.set_instance_fields(link, role="satellite")

    assert link.is_symlink()
    assert config.load(real).instance.role == "satellite"
    assert config.backup_path(link).read_text(encoding="utf-8") == EXISTING


def test_scaffold_over_an_existing_file_keeps_a_backup(tmp_path: Path) -> None:
    path = _write(tmp_path, EXISTING)
    path.chmod(0o600)

    config.scaffold_instance(path, name="Fresh Haus")

    assert config.backup_path(path).read_text(encoding="utf-8") == EXISTING
    assert config.load(path).instance.name == "Fresh Haus"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ─── load_raw ────────────────────────────────────────────────────────


def test_load_raw_reads_without_schema(tmp_path: Path) -> None:
    assert config.load_raw(_write(tmp_path, "# nothing yet\n")) == {}
    assert config.load_raw(_write(tmp_path, "router: 1\n", "r.yaml")) == {"router": 1}


def test_load_raw_reports_bad_files_as_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        config.load_raw(_write(tmp_path, b"\xff\xfe", "bin.yaml"))
    with pytest.raises(ConfigError):
        config.load_raw(_write(tmp_path, "- a\n", "list.yaml"))
    with pytest.raises(ConfigError):
        config.load_raw(tmp_path / "missing.yaml")


def test_load_raw_if_present_starts_a_missing_file_empty_but_never_a_broken_one(
    tmp_path: Path,
) -> None:
    assert config.load_raw_if_present(tmp_path / "missing.yaml") == {}
    assert config.load_raw_if_present(_write(tmp_path, "router: 1\n")) == {"router": 1}
    with pytest.raises(ConfigError, match="not a valid date"):
        config.load_raw_if_present(_write(tmp_path, "since: 2026-02-30\n", "date.yaml"))
    with pytest.raises(ConfigError, match="must be a mapping"):
        config.load_raw_if_present(_write(tmp_path, "- a\n", "list.yaml"))


# ─── values PyYAML parses but cannot build ───────────────────────────
#
# PyYAML raises ValueError / IndexError / KeyError / AttributeError (not
# YAMLError) from its constructors, and RecursionError for runaway nesting.

UNBUILDABLE = {
    "impossible-date": ("since: 2026-02-30\n", "2026-02-30 on line 2 is not a valid date"),
    "empty-int": ('port: !!int ""\n', '!!int "" on line 2 is not a valid whole number'),
    "bool-maybe": ("flag: !!bool maybe\n", "!!bool maybe on line 2 is not a valid true/false"),
    "timestamp-junk": ("when: !!timestamp x\n", "!!timestamp x on line 2 is not a valid date"),
}


@pytest.mark.parametrize("case", sorted(UNBUILDABLE))
def test_an_unbuildable_value_is_refused_with_its_line(tmp_path: Path, case: str) -> None:
    line, pointer = UNBUILDABLE[case]
    before = ("router: 1\n" + line).encode("utf-8")
    path = _write(tmp_path, before)

    error = _refused(path, before, **NEW)

    assert error.message.startswith(f"cannot read {path}: {pointer}")
    assert error.fix is not None
    assert "line 2" in error.fix
    with pytest.raises(ConfigError, match="not a valid"):
        config.load_raw(path)


def test_runaway_nesting_is_refused(tmp_path: Path) -> None:
    before = ("v: " + "[" * 5000 + "]" * 5000 + "\n").encode("utf-8")
    path = _write(tmp_path, before)

    error = _refused(path, before, **NEW)

    assert "nested too deeply" in error.message


@pytest.mark.parametrize("written", ['!!int ""', "!!bool maybe", "2026-02-30", "!!timestamp x"])
def test_an_edit_that_reads_back_unbuildable_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written: str
) -> None:
    """The re-parse guard also survives PyYAML's constructor errors (not only YAMLError)."""
    before = b"router: 1\n"
    path = _write(tmp_path, before)
    monkeypatch.setattr(config, "_yaml_scalar", lambda value: written)

    error = _refused(path, before, name="Box", slug="box")

    assert "did not read back" in error.message
    assert error.fix is not None
    assert "instance:" in error.fix


# ─── read-back cost ──────────────────────────────────────────────────


class _OverBudget(BaseException):
    """Raised by SIGALRM; a BaseException, so no ``except Exception`` can swallow it."""


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs SIGALRM")
def test_alias_doubling_reads_back_in_linear_time(tmp_path: Path) -> None:
    """Every level doubles the tree under it (2**40 leaves); the read-back must not walk it."""
    levels = "".join(f"l{i}: &l{i} [*l{i - 1}, *l{i - 1}]\n" for i in range(1, 41))
    path = _write(tmp_path, "instance:\n  name: Old\n  slug: old\nl0: &l0 [leaf]\n" + levels)

    def over_budget(*_args: object) -> None:
        raise _OverBudget

    previous = signal.signal(signal.SIGALRM, over_budget)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        changes = config.set_instance_fields(path, name="New")
    except _OverBudget:
        pytest.fail("the read-back walked the alias tree and ran past its 5 s budget")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert changes == {"name": ("Old", "New")}
    assert config.load_raw(path)["instance"]["name"] == "New"
