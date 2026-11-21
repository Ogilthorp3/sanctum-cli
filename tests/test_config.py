"""Boundary tests for config loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from sanctum_cli import config
from sanctum_cli.errors import ConfigError


def test_load_minimal(minimal_instance_yaml: Path) -> None:
    cfg = config.load(minimal_instance_yaml)
    assert cfg.instance.name == "Test Instance"
    assert cfg.instance.slug == "test-instance"
    assert cfg.cli.default_provider == "claude"
    assert cfg.cli.routing.fallback == "claude"
    assert cfg.cli.telemetry.enabled is True


def test_load_full(full_instance_yaml: Path) -> None:
    cfg = config.load(full_instance_yaml)
    assert cfg.cli.cloud_backup is not None
    assert cfg.cli.cloud_backup.primary is not None
    assert cfg.cli.cloud_backup.primary.repo == "/Volumes/T9/sanctum-restic"
    assert len(cfg.cli.routing.rules) == 2
    assert cfg.cli.routing.rules[0].then == "gemini"


def test_missing_file_raises_with_fix(tmp_path: Path) -> None:
    target = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "not found" in ei.value.message
    assert ei.value.fix is not None
    assert "instance:" in ei.value.fix


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    target = tmp_path / "bad.yaml"
    target.write_text("instance: : :\n  not valid", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "YAML parse error" in ei.value.message


def test_unknown_key_in_cli_block_rejected(tmp_path: Path) -> None:
    target = tmp_path / "extra.yaml"
    target.write_text(
        "instance:\n  name: t\n  slug: t\ncli:\n  bogus_key: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "bogus_key" in ei.value.message


@pytest.mark.parametrize("tail", ["cli:\n", "cli: ~\n"], ids=["no-value", "tilde"])
def test_an_empty_cli_block_loads_with_every_setting_at_its_default(
    tmp_path: Path, tail: str
) -> None:
    """A ``cli:`` whose keys are all commented out was a schema violation, so every command
    that loads the config, and each cloud setup wizard at its start, refused the file."""
    target = tmp_path / "instance.yaml"
    target.write_text("instance:\n  name: t\n  slug: t\n" + tail, encoding="utf-8")
    assert config.load(target).cli == config.CliConfig()


@pytest.mark.parametrize("value", ["[]", "''", "false", "0", "just text"])
def test_a_cli_entry_that_is_not_a_block_of_keys_is_still_refused(
    tmp_path: Path, value: str
) -> None:
    target = tmp_path / "instance.yaml"
    target.write_text(f"instance:\n  name: t\n  slug: t\ncli: {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "\n  cli: Input should be a valid dictionary" in ei.value.message


def test_invalid_provider_choice_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad-provider.yaml"
    target.write_text(
        "instance:\n  name: t\n  slug: t\ncli:\n  default_provider: openai\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "default_provider" in ei.value.message


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    target = tmp_path / "list.yaml"
    target.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "mapping" in ei.value.message.lower()


def test_env_override_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere.yaml"
    target.write_text("instance:\n  name: e\n  slug: e\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))
    cfg = config.load()
    assert cfg.instance.slug == "e"


def test_telemetry_path_is_expanded(tmp_path: Path) -> None:
    target = tmp_path / "tel.yaml"
    target.write_text(
        "instance:\n  name: t\n  slug: t\ncli:\n  telemetry:\n    path: ~/.sanctum/cli.jsonl\n",
        encoding="utf-8",
    )
    cfg = config.load(target)
    assert "~" not in str(cfg.cli.telemetry.path)


def test_gemini_provider_proxy_config_loads(tmp_path: Path) -> None:
    target = tmp_path / "gemini-proxy.yaml"
    target.write_text(
        "instance:\n  name: t\n  slug: t\ncli:\n"
        "  providers:\n"
        "    gemini:\n"
        "      via: proxy\n"
        "      proxy_url: http://127.0.0.1:6543\n"
        "      model: gemini-best-pro\n",
        encoding="utf-8",
    )
    cfg = config.load(target)
    assert cfg.cli.providers.gemini.via == "proxy"
    assert cfg.cli.providers.gemini.proxy_url == "http://127.0.0.1:6543"
    assert cfg.cli.providers.gemini.model == "gemini-best-pro"


def test_gemini_provider_default_via_is_direct(minimal_instance_yaml: Path) -> None:
    cfg = config.load(minimal_instance_yaml)
    assert cfg.cli.providers.gemini.via == "direct"
    assert cfg.cli.providers.gemini.proxy_url == "http://127.0.0.1:6543"


def test_gemini_provider_invalid_via_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad-gemini-via.yaml"
    target.write_text(
        "instance:\n  name: t\n  slug: t\ncli:\n  providers:\n    gemini:\n      via: magic\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "gemini" in ei.value.message


@pytest.mark.parametrize(
    "tricky_name",
    [
        "Home: 2026",
        "true",
        "123",
        "Evil\ncli:\n  default_provider: gemini",
    ],
)
def test_scaffold_instance_safe_yaml_quoting_and_no_injection(
    tmp_path: Path, tricky_name: str
) -> None:
    target = tmp_path / "instance.yaml"
    config.scaffold_instance(target, name=tricky_name)
    cfg = config.load(target)
    assert cfg.instance.name == tricky_name
    assert cfg.cli.default_provider == "claude"


def test_scaffold_instance_whitespace_only_falls_back_to_default(tmp_path: Path) -> None:
    target = tmp_path / "instance.yaml"
    config.scaffold_instance(target, name="   \t\n  ")
    cfg = config.load(target)
    assert cfg.instance.name
    assert cfg.instance.slug


# ─── files PyYAML reads but cannot build, or cannot read at all ─────
#
# PyYAML's constructors raise plain Python errors, not YAMLError, for a value its
# tag cannot hold. ``load`` must turn every one into a ConfigError that points at
# the value; ``instance_value`` / ``instance_role`` must return their default.

_HEAD = "instance:\n  name: Box\n  slug: box\n"
_DEEP = "v: " + "[" * 5000 + "]" * 5000 + "\n"

UNBUILDABLE = [
    pytest.param(
        _HEAD + "since: 2026-02-30\n",
        "2026-02-30 on line 4 is not a valid date (day is out of range for month)",
        id="impossible-date",
    ),
    pytest.param(
        _HEAD + 'port: !!int ""\n', '!!int "" on line 4 is not a valid whole number', id="empty-int"
    ),
    pytest.param(
        _HEAD + "flag: !!bool maybe\n",
        "!!bool maybe on line 4 is not a valid true/false value",
        id="bool-maybe",
    ),
    pytest.param(
        _HEAD + "when: !!timestamp soon\n",
        "!!timestamp soon on line 4 is not a valid date",
        id="timestamp-junk",
    ),
    pytest.param(
        _HEAD + "seen:\n  - ok\n  - 2026-13-45\n",
        "2026-13-45 on line 6 is not a valid date (month must be in 1..12)",
        id="date-in-a-list",
    ),
]


@pytest.mark.parametrize(("body", "pointer"), UNBUILDABLE)
def test_load_points_at_a_value_pyyaml_cannot_build(
    tmp_path: Path, body: str, pointer: str
) -> None:
    target = tmp_path / "instance.yaml"
    target.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert ei.value.message == f"cannot read {target}: {pointer}"
    assert ei.value.fix is not None
    assert "line" in ei.value.fix


def test_load_reports_runaway_nesting(tmp_path: Path) -> None:
    target = tmp_path / "instance.yaml"
    target.write_text(_DEEP, encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert "nested too deeply" in ei.value.message
    assert ei.value.fix


def test_load_reports_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    target = tmp_path / "instance.yaml"
    target.write_bytes("instance:\n  name: Café\n  slug: cafe\n".encode("latin-1"))
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert ei.value.message == f"{target} is not UTF-8 text (undecodable byte at offset 21)"
    assert ei.value.fix is not None
    assert "UTF-8" in ei.value.fix


def test_load_reports_a_directory_in_place_of_the_file(tmp_path: Path) -> None:
    target = tmp_path / "instance.yaml"
    target.mkdir()
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    assert ei.value.message.startswith(f"cannot read {target}")


@pytest.mark.parametrize(
    "body",
    [
        *(case.values[0] for case in UNBUILDABLE),
        _HEAD.replace("Box", "Café").encode("latin-1"),
        _DEEP,
    ],
)
def test_instance_value_falls_back_on_any_unreadable_file(
    tmp_path: Path, body: str | bytes
) -> None:
    """It feeds import-time code (council's seats): it must never raise."""
    target = tmp_path / "instance.yaml"
    if isinstance(body, bytes):
        target.write_bytes(body)
    else:
        target.write_text(body, encoding="utf-8")
    assert config.instance_value("instance.name", "fallback", path=target) == "fallback"


@pytest.mark.parametrize(("body", "_pointer"), UNBUILDABLE)
def test_instance_role_reads_past_a_value_pyyaml_cannot_build(
    tmp_path: Path, body: str, _pointer: str
) -> None:
    """The role is read off the composed node tree, so a bad value elsewhere cannot hide it."""
    target = tmp_path / "instance.yaml"
    target.write_text(body.replace("slug: box\n", "slug: box\n  role: satellite\n"), "utf-8")
    assert config.instance_value("instance.role", "fallback", path=target) == "fallback"
    assert config.instance_role(path=target) == "satellite"


def test_instance_role_is_none_for_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    """Nothing to read the role from: the file is not text."""
    target = tmp_path / "instance.yaml"
    target.write_bytes(_HEAD.replace("Box", "Café").encode("latin-1") + b"  role: satellite\n")
    assert config.instance_role(path=target) is None


@pytest.mark.parametrize(
    "tail", [_DEEP, "cli: [unclosed\n"], ids=["runaway-nesting", "syntax-error"]
)
def test_instance_role_reads_a_file_that_does_not_compose_up_to_its_first_error(
    tmp_path: Path, tail: str
) -> None:
    """The role is read off the parser's events, which come before the error."""
    target = tmp_path / "instance.yaml"
    target.write_text(_HEAD + "  role: satellite\n" + tail, encoding="utf-8")
    with pytest.raises(ConfigError):
        config.load(target)
    assert config.instance_role(path=target) == "satellite"


def test_the_runaway_nesting_fix_names_the_file_being_read(tmp_path: Path) -> None:
    devices = tmp_path / "devices.yaml"
    devices.write_text(_DEEP, encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.read_yaml(devices)
    assert ei.value.message == f"cannot read {devices}: its YAML is nested too deeply"
    assert ei.value.fix == "flatten it: devices.yaml needs only a few levels of keys"


def test_an_unknown_user_in_the_instance_file_path_is_kept_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pathlib raises RuntimeError expanding ``~nosuchuser``; the readers must not."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", "~sanctum-nosuchuser/instance.yaml")
    assert config.instance_path() == Path("~sanctum-nosuchuser/instance.yaml")
    assert config.instance_role() is None
    assert config.instance_value("instance.name", "fallback") == "fallback"
    with pytest.raises(ConfigError, match="instance config not found: ~sanctum-nosuchuser"):
        config.load()


def _load_error(tmp_path: Path, body: str) -> ConfigError:
    target = tmp_path / "instance.yaml"
    target.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.load(target)
    return ei.value


def test_a_merge_key_is_never_the_value_blamed(tmp_path: Path) -> None:
    """``<<`` cannot be built on its own, so it must not be reported in place of the date."""
    error = _load_error(
        tmp_path,
        "defaults: &defaults\n  timezone: UTC\n"
        "instance:\n  <<: *defaults\n  name: Box\n  slug: box\n"
        "since: 2026-02-30\n",
    )
    assert error.message == (
        f"cannot read {tmp_path / 'instance.yaml'}: 2026-02-30 on line 7 is not a valid date "
        "(day is out of range for month)"
    )
    assert error.fix is not None
    assert "line 7" in error.fix


def test_the_first_bad_value_is_reported_with_its_own_reason(tmp_path: Path) -> None:
    """safe_load stops at the later top-level date first (it builds nested blocks last)."""
    error = _load_error(tmp_path, _HEAD + "seen:\n  first: 2026-02-30\nlast: 2026-13-01\n")
    assert error.message == (
        f"cannot read {tmp_path / 'instance.yaml'}: 2026-02-30 on line 5 is not a valid date "
        "(day is out of range for month)"
    )


def test_a_value_that_fails_with_a_yaml_error_alone_is_passed_over(tmp_path: Path) -> None:
    """``!!binary abc`` fails with a YAMLError on its own; safe_load failed on the date."""
    error = _load_error(tmp_path, _HEAD + "nested:\n  blob: !!binary abc\nlast: 2026-13-01\n")
    assert error.message == (
        f"cannot read {tmp_path / 'instance.yaml'}: 2026-13-01 on line 6 is not a valid date "
        "(month must be in 1..12)"
    )


def test_read_yaml_parses_any_top_level_and_reports_like_load_raw(tmp_path: Path) -> None:
    """For devices.yaml and the like: the caller checks the top level, errors read the same."""
    target = tmp_path / "devices.yaml"
    target.write_text("# nothing yet\n", encoding="utf-8")
    assert config.read_yaml(target) is None
    target.write_text("- a\n- b\n", encoding="utf-8")
    assert config.read_yaml(target) == ["a", "b"]
    target.write_text("family: {}\nsince: 2026-02-30\n", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        config.read_yaml(target)
    assert ei.value.message == (
        f"cannot read {target}: 2026-02-30 on line 2 is not a valid date "
        "(day is out of range for month)"
    )
