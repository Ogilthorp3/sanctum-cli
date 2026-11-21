"""``instance.role`` is the single role detector, and every tier check follows it.

A satellite (a second haus) runs its own local brain but none of the hub
services, yet it can carry hub-looking artifacts (model manifests, even a stray
proxyd plist). ``sanctum doctor`` and ``sanctum service-user`` must grade it on
what it declares, never fail it for missing wave-1 hub daemons, and never tell
its operator to install them. A declared hub is graded as a hub before its
services leave any artifact on disk.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from sanctum_cli import config
from sanctum_cli import service_user as su
from sanctum_cli.cli import app
from sanctum_cli.commands import (
    onboard,
    self_test,
    service_user_cmd,  # noqa: F401 — registers the subcommand
    setup,
)
from sanctum_cli.errors import ConfigError, ExitCode
from sanctum_cli.providers.base import HealthSnapshot, Provider

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result

runner = CliRunner()

SATELLITE = (
    "instance:\n  name: Second Haus\n  slug: second-haus\n  role: satellite\n"
    "router:\n  default: local\n  backends:\n    local:\n      url: http://127.0.0.1:1338/v1\n"
)
HUB = "instance:\n  name: Hub\n  slug: hub\n  role: hub\n"
NO_ROLE = "instance:\n  name: Box\n  slug: box\n"


def _instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str | bytes) -> Path:
    target = tmp_path / "instance.yaml"
    if isinstance(body, bytes):
        target.write_bytes(body)
    else:
        target.write_text(body, encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(target))
    return target


def _hub_looking_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What a satellite may legitimately carry: manifests, a proxyd dir, a stray plist."""
    for marker in (".sanctum/manifests", ".sanctum/sanctum-proxy"):
        (Path.home() / marker).mkdir(parents=True, exist_ok=True)
    daemons = tmp_path / "LaunchDaemons"
    daemons.mkdir()
    (daemons / "haus.sanctum.proxyd.plist").write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(su, "DAEMON_DIR", daemons)


# ── config.instance_role / instance_value ─────────────────────────────


@pytest.mark.parametrize(
    ("body", "role"),
    [(SATELLITE, "satellite"), (HUB, "hub"), (NO_ROLE, None)],
)
def test_instance_role_reads_the_declared_role(tmp_path, monkeypatch, body, role) -> None:
    _instance(tmp_path, monkeypatch, body)
    assert config.instance_role() == role


@pytest.mark.parametrize("value", ["Satellite", "edge", "[hub]", "1"])
def test_instance_role_ignores_values_the_schema_rejects(tmp_path, monkeypatch, value) -> None:
    """Only the schema's own roles count; anything else falls back to inference."""
    target = _instance(tmp_path, monkeypatch, f"instance:\n  name: B\n  slug: b\n  role: {value}\n")
    assert config.instance_role() is None
    with pytest.raises(ConfigError):
        config.load(target)


def test_instance_role_survives_an_otherwise_invalid_file(tmp_path, monkeypatch) -> None:
    """The role still applies when another key fails validation (self-test reports that)."""
    target = _instance(
        tmp_path, monkeypatch, SATELLITE + "cli:\n  default_provider: not-a-provider\n"
    )
    with pytest.raises(ConfigError):
        config.load(target)
    assert config.instance_role() == "satellite"


def test_instance_role_is_none_without_a_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    assert config.instance_role() is None


def test_instance_value_returns_the_default_for_a_non_utf8_file(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, b"instance:\n  name: \xff\xfe\n  role: satellite\n")
    assert config.instance_value("instance.role", "fallback") == "fallback"
    assert config.instance_role() is None


def test_instance_value_returns_the_default_for_an_impossible_date(tmp_path, monkeypatch) -> None:
    """PyYAML raises ValueError (not YAMLError) building a timestamp like 2026-13-45."""
    _instance(tmp_path, monkeypatch, "instance:\n  role: satellite\nsince: 2026-13-45\n")
    assert config.instance_value("instance.role", "fallback") == "fallback"


# ── a value PyYAML cannot build never hides the declared role ─────────

BAD_LINE = "since: 2026-02-30\n"

UNBUILDABLE_ELSEWHERE = {
    "a-date-after-it": SATELLITE + BAD_LINE,
    "a-date-before-it": BAD_LINE + SATELLITE,
    "a-date-in-its-block": "instance:\n  since: 2026-02-30\n  role: satellite\n",
    "a-bad-bool": SATELLITE + "flag: !!bool maybe\n",
    "an-empty-int": SATELLITE + "port: !!int ''\n",
    "an-unhashable-key": SATELLITE + "? [a, b]\n: c\n",
}


@pytest.mark.parametrize("case", sorted(UNBUILDABLE_ELSEWHERE))
def test_instance_role_survives_a_value_pyyaml_cannot_build(tmp_path, monkeypatch, case) -> None:
    """The role used to read as unset here, so a satellite was graded and gated as a hub."""
    target = _instance(tmp_path, monkeypatch, UNBUILDABLE_ELSEWHERE[case])
    with pytest.raises(ConfigError):
        config.load_raw(target)  # the file really does not load
    assert config.instance_role() == "satellite"


@pytest.mark.parametrize(
    ("body", "role"),
    [
        pytest.param("instance:\n  role: satellite\n  role: hub\n", "hub", id="last-key-wins"),
        pytest.param(
            "instance:\n  role: hub\ninstance:\n  role: satellite\n",
            "satellite",
            id="last-block-wins",
        ),
        pytest.param("instance:\n  role: satellite\ninstance: ~\n", None, id="last-block-is-null"),
        pytest.param("instance:\n  role: 'satellite'\n", "satellite", id="single-quoted"),
        pytest.param('instance:\n  role: "hub"\n', "hub", id="double-quoted"),
        pytest.param("instance:\n  role: !!str satellite\n", "satellite", id="str-tag"),
        pytest.param("site: &r satellite\ninstance:\n  role: *r\n", "satellite", id="alias"),
        pytest.param("base: &b {role: satellite}\ninstance:\n  <<: *b\n", "satellite", id="merged"),
        pytest.param(
            "base: &b {role: satellite}\ninstance:\n  <<: *b\n  role: hub\n",
            "hub",
            id="explicit-beats-merged",
        ),
        pytest.param("<<: {instance: {role: satellite}}\n", "satellite", id="merged-block"),
        pytest.param("instance: !!map {role: satellite}\n", "satellite", id="map-tag"),
        pytest.param("instance: !!set {role: satellite}\n", None, id="a-set"),
        pytest.param("--- !!set\n? instance\n: {role: satellite}\n", None, id="a-set-at-the-top"),
        pytest.param("instance:\n  role: Satellite\n", None, id="wrong-case"),
        pytest.param("instance:\n  role: [satellite]\n", None, id="a-list"),
        pytest.param("instance:\n  role: !!int 1\n", None, id="a-number"),
        pytest.param("instance: satellite\n", None, id="instance-is-a-value"),
        pytest.param("instance:\n  name: Box\n", None, id="no-role"),
    ],
)
def test_past_a_bad_value_the_role_is_the_one_safe_load_reads(
    tmp_path, monkeypatch, body, role
) -> None:
    """Off the node tree, the role is what safe_load reads from the file without the bad line."""
    target = _instance(tmp_path, monkeypatch, body)
    assert config.instance_role() == role
    target.write_text(body + BAD_LINE, encoding="utf-8")
    assert config.instance_role() == role


@pytest.mark.parametrize("sources", ["[*a, *b]", "[*b, *a]"])
def test_past_a_bad_value_merged_roles_resolve_as_pyyaml_merges_them(
    tmp_path, monkeypatch, sources
) -> None:
    body = f"a: &a {{role: hub}}\nb: &b {{role: satellite}}\ninstance:\n  <<: {sources}\n"
    target = _instance(tmp_path, monkeypatch, body)
    merged = config.instance_role()
    assert merged in ("hub", "satellite")
    target.write_text(body + BAD_LINE, encoding="utf-8")
    assert config.instance_role() == merged


@pytest.mark.parametrize(
    "body",
    ["<<: 5\n" + SATELLITE, "instance:\n  <<: 5\n  role: satellite\n"],
    ids=["at-the-top", "in-the-block"],
)
def test_a_malformed_merge_does_not_hide_the_role(tmp_path, monkeypatch, body) -> None:
    """safe_load rejects a ``<<`` that is not a mapping; the explicit keys still count."""
    target = _instance(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError):
        config.load_raw(target)
    assert config.instance_role() == "satellite"


# ── a file that does not even compose: the role before the first error ──

BROKEN_AFTER_THE_ROLE = {
    "a-tab-indent": "\tcli: x\n",
    "an-unclosed-list": "cli: [unclosed\n",
    "an-undefined-alias": "cli: *nowhere\n",
    "a-second-document": "---\nother: document\n",
}


@pytest.mark.parametrize("case", sorted(BROKEN_AFTER_THE_ROLE))
def test_instance_role_reads_up_to_the_first_syntax_error(tmp_path, monkeypatch, case) -> None:
    """The role used to read as unset whenever the file did not compose."""
    target = _instance(tmp_path, monkeypatch, SATELLITE + BROKEN_AFTER_THE_ROLE[case])
    with pytest.raises(ConfigError):
        config.load_raw(target)  # the file really does not load
    assert config.instance_role() == "satellite"


@pytest.mark.parametrize(
    "body",
    [
        "\tcli: x\n" + SATELLITE,
        "instance:\n\tname: Second Haus\n  role: satellite\n",
        "cli: [unclosed\n" + SATELLITE,
        "other: document\n---\n" + SATELLITE,
    ],
    ids=[
        "tab-indent-before",
        "tab-indent-in-its-block",
        "unclosed-list-before",
        "only-in-a-second-document",
    ],
)
def test_a_role_after_the_first_syntax_error_cannot_be_read(tmp_path, monkeypatch, body) -> None:
    _instance(tmp_path, monkeypatch, body)
    assert config.instance_role() is None


@pytest.mark.parametrize(
    ("body", "role"),
    [
        pytest.param(
            "instance:\n  role:\n    satellite\t# set by hand\n",
            "satellite",
            id="a-tab-after-it-on-its-own-line",
        ),
        pytest.param(
            "instance:\n  role:\n    'satellite'\n'note\n",
            "satellite",
            id="an-unclosed-quote-on-the-next-line",
        ),
        pytest.param(
            "instance:\n  role: satellite\t# note\n", "satellite", id="a-tab-and-a-comment-after-it"
        ),
        pytest.param("instance:\n  role: hub\tx\n", "hub", id="a-tab-and-text-after-it"),
        pytest.param("instance: {role: hub?x}\n", "hub", id="a-question-mark-inside-braces"),
        pytest.param(
            "instance:\n  role: satellite\n  role: hub: x\n",
            "hub",
            id="a-colon-after-the-last-role",
        ),
        pytest.param("instance: {role: hub]\n", "hub", id="a-stray-bracket-inside-braces"),
        pytest.param('instance:\n  role: "satellite\n', None, id="an-unclosed-quote-in-its-value"),
    ],
)
def test_the_role_is_read_up_to_where_the_first_error_starts(
    tmp_path, monkeypatch, body, role
) -> None:
    """Up to the error itself, not the start of its line: an unclosed quote reported at the
    end of the file used to hide all of it. A value the error cuts off (an unclosed quote) is
    not a role, but a plain value ends where PyYAML ends it, before the error: at a tab or a
    ``: ``, and inside braces also at ``,``, ``?``, ``[``, ``]``, ``{`` or ``}``."""
    target = _instance(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError):
        config.load_raw(target)  # the file really does not load
    assert config.instance_role() == role


@pytest.mark.parametrize(
    ("body", "role"),
    [
        pytest.param(
            "instance:\n  role: satellite  # \x1b[32mset by init\x1b[0m\n",
            "satellite",
            id="escape-codes-in-its-comment",
        ),
        pytest.param(
            "# \x1b[1mheader\x1b[0m\ninstance:\n  role: satellite\n",
            "satellite",
            id="escape-codes-in-a-comment-before-it",
        ),
        pytest.param("note: bell\x07\n" + SATELLITE, "satellite", id="one-in-a-value-before-it"),
        pytest.param(SATELLITE + "note: bell\x07\n", "satellite", id="one-in-a-value-after-it"),
        pytest.param(
            "instance:\n  role: hub\n  role: satellite  # \x07\n",
            "satellite",
            id="a-control-character-in-the-last-roles-comment",
        ),
        pytest.param(
            "instance:\n  role: hub\n  name: caf\x07e\n  role: satellite\n",
            "satellite",
            id="one-in-a-value-between-two-roles",
        ),
        pytest.param(
            "site: &r satellite\ninstance:\n  role: *r\n# \x1b[0m\n",
            "satellite",
            id="an-aliased-role",
        ),
        pytest.param(
            "base: &b {role: satellite}\ninstance:\n  <<: *b\n# \x1b[0m\n",
            "satellite",
            id="a-merged-role",
        ),
        pytest.param("instance:\n  role: satellite\x07\n", None, id="one-at-the-end-of-its-value"),
        pytest.param(
            "instance:\n  role: hub\n    ext\x07ra\n",
            None,
            id="a-value-that-runs-into-a-control-character",
        ),
        pytest.param(
            "instance:\n  role: hub\n  role: satellite\x07\n",
            None,
            id="a-last-role-that-runs-into-a-control-character",
        ),
        pytest.param("inst\x07ance:\n  role: satellite\n", None, id="one-in-the-instance-key"),
        pytest.param(
            "instance:\n  role: hub\n  ro\x07le: satellite\n", "hub", id="one-in-the-last-role-key"
        ),
        pytest.param(
            "x: 'a'\x07\n" + SATELLITE, None, id="one-right-after-a-quoted-value-before-it"
        ),
    ],
)
def test_a_character_pyyaml_refuses_is_read_as_an_unknown_character(
    tmp_path, monkeypatch, body, role
) -> None:
    """PyYAML refuses the whole file over one such character; the role reader reads each as
    U+FFFD, which is never part of hub, satellite, role or instance. One in a comment, or in
    another key or plain or quoted value, hides nothing (the text used to end at the first one,
    hiding every role after it, and an aliased or merged role before it); one in the role's
    value or key breaks that value or key, and a broken key is another key. Where U+FFFD cannot
    stand (right after a quoted value, say) it is a syntax error, so a role after it is not read."""
    target = _instance(tmp_path, monkeypatch, body)
    with pytest.raises(ConfigError):
        config.load_raw(target)  # the file really does not load
    assert config.instance_role() == role


@pytest.mark.parametrize(
    ("body", "role"),
    [
        pytest.param("instance:\n  role: satellite\n  role: hub\n", "hub", id="last-key-wins"),
        pytest.param(
            "instance:\n  role: hub\ninstance:\n  role: satellite\n",
            "satellite",
            id="last-block-wins",
        ),
        pytest.param("instance:\n  role: satellite\ninstance: ~\n", None, id="last-block-is-null"),
        pytest.param("instance:\n  role: satellite\n  role: [hub]\n", None, id="last-is-a-list"),
        pytest.param("instance:\n  role: 'satellite'\n", "satellite", id="single-quoted"),
        pytest.param('"instance":\n  "role": "hub"\n', "hub", id="quoted-keys"),
        pytest.param("instance:\n  role: !!str satellite\n", "satellite", id="str-tag"),
        pytest.param("instance:\n  role: ! satellite\n", "satellite", id="non-specific-tag"),
        pytest.param("!!str instance:\n  !!str role: hub\n", "hub", id="str-tagged-keys"),
        pytest.param("instance: !!map {role: satellite}\n", "satellite", id="map-tag"),
        pytest.param("instance: {role: satellite}\n", "satellite", id="flow-block"),
        pytest.param("instance:\n  role: !mine satellite\n", None, id="another-tag"),
        pytest.param("!mine instance:\n  role: satellite\n", None, id="another-tag-on-the-key"),
        pytest.param("instance: !!set {role: satellite}\n", None, id="a-set"),
        pytest.param("instance:\n  role: Satellite\n", None, id="wrong-case"),
        pytest.param("instance:\n  site:\n    role: satellite\n", None, id="one-level-too-deep"),
        pytest.param("role: satellite\n", None, id="at-the-top"),
        pytest.param("- instance:\n    role: satellite\n", None, id="a-list-at-the-top"),
        pytest.param("site: &r satellite\ninstance:\n  role: *r\n", None, id="an-alias"),
        pytest.param("b: &b {role: satellite}\ninstance:\n  <<: *b\n", None, id="a-merge"),
        pytest.param(
            "instance:\n  role:\n    satellite  # set by hand\n", "satellite", id="on-its-own-line"
        ),
        pytest.param("{instance: {role: hub}}  # all on one line\n", "hub", id="flow-at-the-top"),
    ],
)
def test_past_a_syntax_error_the_role_is_read_off_the_parser_events(
    tmp_path, monkeypatch, body, role
) -> None:
    """The last ``role`` scalar of the last top-level ``instance`` block, as written.

    Aliases and ``<<`` merges are not followed there. A role on the line just
    before the error still counts, even where PyYAML reads that far ahead.
    """
    _instance(tmp_path, monkeypatch, body + "\tbroken: line\n")
    assert config.instance_role() == role


@pytest.mark.parametrize(
    "reader",
    [
        lambda: su.haus_tier_present() is False,
        lambda: su.check_wave1().reason.startswith("satellite"),
        lambda: self_test._satellite_role() is True,
        lambda: self_test._haus_tier_installed() is False,
        lambda: self_test._tier_label() == "satellite",
        lambda: setup._tier() == "basic",
    ],
    ids=["haus_tier_present", "check_wave1", "satellite_role", "haus_tier", "tier_label", "setup"],
)
@pytest.mark.parametrize("broken", [BAD_LINE, "\tcli: x\n"], ids=["bad-value", "syntax-error"])
def test_every_role_reader_sees_a_satellite_past_a_bad_value(
    tmp_path, monkeypatch, reader: Callable[[], bool], broken: str
) -> None:
    _hub_looking_artifacts(tmp_path, monkeypatch)
    _instance(tmp_path, monkeypatch, SATELLITE + broken)
    assert reader()


# ── service_user.haus_tier_present / check_wave1 ──────────────────────


def test_haus_tier_present_is_false_on_a_satellite_with_hub_artifacts(
    tmp_path, monkeypatch
) -> None:
    _hub_looking_artifacts(tmp_path, monkeypatch)
    _instance(tmp_path, monkeypatch, SATELLITE)
    assert su.haus_tier_present() is False


def test_haus_tier_present_is_true_on_a_declared_hub_without_artifacts(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(su, "DAEMON_DIR", tmp_path / "no-daemons")
    _instance(tmp_path, monkeypatch, HUB)
    assert su.haus_tier_present() is True


def test_haus_tier_present_infers_from_artifacts_without_a_role(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(su, "DAEMON_DIR", tmp_path / "no-daemons")
    _instance(tmp_path, monkeypatch, NO_ROLE)
    assert su.haus_tier_present() is False
    (Path.home() / ".sanctum/manifests").mkdir(parents=True)
    assert su.haus_tier_present() is True


def test_check_wave1_on_a_satellite_is_not_applicable_and_says_why(tmp_path, monkeypatch) -> None:
    _hub_looking_artifacts(tmp_path, monkeypatch)
    _instance(tmp_path, monkeypatch, SATELLITE)
    monkeypatch.setattr(su, "_process_owner", _must_not_run("ps"))
    report = su.check_wave1()
    assert report.applicable is False
    assert report.ok is True
    assert report.reason == "satellite — the wave-1 hub daemons run on the hub, not here"


def test_check_wave1_without_a_role_or_artifacts_keeps_the_cli_only_reason(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(su, "DAEMON_DIR", tmp_path / "no-daemons")
    _instance(tmp_path, monkeypatch, NO_ROLE)
    assert su.check_wave1().reason == "CLI-only install — service principal not expected"


def _must_not_run(what: str) -> Callable[..., object]:
    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError(f"{what} must not run on a satellite")

    return boom


# ── sanctum service-user on a satellite ───────────────────────────────


@pytest.mark.parametrize("command", ["check", "status"])
def test_service_user_on_a_satellite_reports_na_not_failure(tmp_path, monkeypatch, command) -> None:
    _hub_looking_artifacts(tmp_path, monkeypatch)
    _instance(tmp_path, monkeypatch, SATELLITE)
    monkeypatch.setattr(su, "_process_owner", _must_not_run("ps"))
    monkeypatch.setattr(su, "_http_status", _must_not_run("an HTTP probe"))
    result = runner.invoke(app, ["service-user", command])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "n/a" in out
    assert "satellite" in out
    assert "FAIL" not in out
    assert "service-user install" not in out


def test_service_user_check_on_a_declared_hub_still_fails_and_points_at_install(
    tmp_path, monkeypatch
) -> None:
    _instance(tmp_path, monkeypatch, HUB)
    monkeypatch.setattr(su, "DAEMON_DIR", tmp_path / "no-daemons")
    monkeypatch.setattr(su, "service_user_exists", lambda name="sanctum": False)
    monkeypatch.setattr(su, "_process_owner", lambda _pattern: None)
    monkeypatch.setattr(su, "_http_status", lambda *_a, **_k: 0)
    result = runner.invoke(app, ["service-user", "check"])
    assert result.exit_code == 1
    assert "sanctum service-user install" in " ".join(result.output.split())


# ── sanctum service-user install is hub-only ──────────────────────────


@pytest.mark.parametrize("args", [[], ["--dry-run"]])
def test_service_user_install_refuses_on_a_satellite(tmp_path, monkeypatch, args) -> None:
    _hub_looking_artifacts(tmp_path, monkeypatch)
    target = _instance(tmp_path, monkeypatch, SATELLITE)
    monkeypatch.setattr(su, "run_install", _must_not_run("the install"))
    monkeypatch.setattr(su, "materialize_assets", _must_not_run("materializing the plists"))

    result = runner.invoke(app, ["service-user", "install", *args])

    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert isinstance(result.exception, SystemExit)  # a clean error, not a traceback
    out = " ".join(result.output.split())
    assert out.startswith("error: the hive service principal")
    assert "is hub-only" in out
    assert f"(instance.role: satellite in {target})" in out
    assert "fix: run it on the hub" in out
    assert not (Path.home() / ".sanctum/launchdaemons").exists()


def test_service_user_install_on_a_declared_hub_still_runs(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, HUB)
    calls: list[bool] = []
    monkeypatch.setattr(su, "run_install", lambda dry_run=False: calls.append(dry_run) or 0)
    healthy = su.Wave1Report(applicable=True, items=[su.CheckItem("all", True, "ok")])
    monkeypatch.setattr(su, "check_wave1", lambda **_k: healthy)

    result = runner.invoke(app, ["service-user", "install"])

    assert result.exit_code == 0, result.output
    assert calls == [False]


def test_service_user_install_failure_is_one_clean_error(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, HUB)
    monkeypatch.setattr(su, "run_install", lambda dry_run=False: 3)

    result = runner.invoke(app, ["service-user", "install"])

    assert result.exit_code == ExitCode.LOCAL_ERROR
    assert isinstance(result.exception, SystemExit)
    assert "error: service-user install exited 3" in result.output


@pytest.mark.parametrize("args", [[], ["--dry-run"]])
def test_service_user_install_fails_closed_on_a_file_that_does_not_load(
    tmp_path, monkeypatch, args
) -> None:
    """A file that does not load installs nothing, whatever its role: exit 5, and nothing runs."""
    target = _instance(tmp_path, monkeypatch, SATELLITE + "since: 2026-02-30\n")
    assert config.instance_role() == "satellite"  # read past the bad value, yet the load fails
    monkeypatch.setattr(su, "run_install", _must_not_run("the install"))
    monkeypatch.setattr(su, "materialize_assets", _must_not_run("materializing the plists"))

    result = runner.invoke(app, ["service-user", "install", *args])

    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert isinstance(result.exception, SystemExit)  # a clean error, not a traceback
    line = SATELLITE.count("\n") + 1
    assert result.stderr.splitlines() == [
        f"error: cannot read {target}: 2026-02-30 on line {line} is not a valid date "
        "(day is out of range for month)",
        f"fix: correct the value on line {line}, or quote it to keep it as text, then re-run",
    ]
    assert not (Path.home() / ".sanctum/launchdaemons").exists()


def _no_install_step_may_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(su.subprocess, "call", _must_not_run("sudo"))
    for step in (
        "materialize_assets",
        "_ensure_group_and_user",
        "_acl_operator_tree",
        "_install_plists",
    ):
        monkeypatch.setattr(su, step, _must_not_run(step))


@pytest.mark.parametrize("euid", [501, 0], ids=["user", "root"])
def test_the_module_entry_point_refuses_on_a_satellite(tmp_path, monkeypatch, capsys, euid) -> None:
    """``python -m sanctum_cli.service_user install`` (sudo re-enters through it) refuses too."""
    target = _instance(tmp_path, monkeypatch, SATELLITE)
    _no_install_step_may_run(monkeypatch)
    monkeypatch.setattr(su.os, "geteuid", lambda: euid)
    monkeypatch.setattr("sys.argv", ["sanctum_cli.service_user", "install"])

    with pytest.raises(SystemExit) as exited:
        su.main()

    assert exited.value.code == ExitCode.CONFIG_ERROR
    err = " ".join(capsys.readouterr().err.split())
    assert err.startswith("error: the hive service principal")
    assert f"(instance.role: satellite in {target})" in err
    assert "fix: run it on the hub" in err


def test_root_reads_the_operators_instance_file_when_sudo_dropped_the_variable(
    tmp_path, monkeypatch
) -> None:
    """sudo resets the environment: as root the role comes from the operator's own file."""
    operators = su.operator_home() / ".sanctum" / "instance.yaml"
    operators.write_text(SATELLITE, encoding="utf-8")
    monkeypatch.delenv("SANCTUM_INSTANCE_FILE")
    # The import-time default would be the developer's real file; keep it out of reach.
    monkeypatch.setattr(config, "DEFAULT_INSTANCE_FILE", tmp_path / "not-a-home" / "x.yaml")
    monkeypatch.setattr(su.os, "geteuid", lambda: 0)
    _no_install_step_may_run(monkeypatch)

    with pytest.raises(ConfigError, match="is hub-only") as refused:
        su.install_wave1_as_root()

    assert f"(instance.role: satellite in {operators})" in refused.value.message


def test_run_install_fails_closed_on_a_file_that_does_not_load(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, HUB + "since: 2026-02-30\n")
    _no_install_step_may_run(monkeypatch)
    monkeypatch.setattr(su.os, "geteuid", lambda: 501)
    for dry_run in (False, True):
        with pytest.raises(ConfigError, match="is not a valid date"):
            su.run_install(dry_run=dry_run)


# ── onboard's service-principal gate ──────────────────────────────────


def _onboard_console(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(onboard, "console", Console(file=buf, width=200))
    return buf


@pytest.mark.parametrize("yes", [True, False])
def test_onboard_service_user_gate_is_na_on_a_satellite(tmp_path, monkeypatch, yes) -> None:
    """No plists written, no prompt, no install hint: the principal is the hub's."""
    _hub_looking_artifacts(tmp_path, monkeypatch)
    _instance(tmp_path, monkeypatch, SATELLITE)
    monkeypatch.setattr(su, "materialize_assets", _must_not_run("materializing the plists"))
    monkeypatch.setattr(su, "run_install", _must_not_run("the install"))
    monkeypatch.setattr(onboard.typer, "confirm", _must_not_run("an install prompt"))
    buf = _onboard_console(monkeypatch)

    assert onboard._run_service_user_install(yes=yes) is False  # nothing configured

    out = buf.getvalue()
    assert "  n/a  satellite — the wave-1 hub daemons run on the hub, not here" in out
    assert "service-user install" not in out
    assert not (Path.home() / ".sanctum/launchdaemons").exists()


def test_onboard_service_user_gate_is_na_without_a_role_or_hub_artifacts(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(su, "DAEMON_DIR", tmp_path / "no-daemons")
    _instance(tmp_path, monkeypatch, NO_ROLE)
    monkeypatch.setattr(su, "materialize_assets", _must_not_run("materializing the plists"))
    buf = _onboard_console(monkeypatch)

    assert onboard._run_service_user_install(yes=True) is False

    out = buf.getvalue()
    assert "  n/a  CLI-only install — service principal not expected" in out
    assert "sanctum init --role hub" in out  # how a brand-new hub gets the principal


def test_onboard_service_user_gate_still_offers_the_install_on_a_hub(tmp_path, monkeypatch) -> None:
    _instance(tmp_path, monkeypatch, HUB)
    failing = su.Wave1Report(applicable=True, items=[su.CheckItem("sanctum user exists", False)])
    monkeypatch.setattr(su, "check_wave1", lambda **_k: failing)
    materialized: list[bool] = []
    monkeypatch.setattr(su, "materialize_assets", lambda *_a, **_k: materialized.append(True))
    buf = _onboard_console(monkeypatch)

    assert onboard._run_service_user_install(yes=True) is False

    assert materialized == [True]
    assert "sanctum service-user install" in buf.getvalue()


# ── sanctum doctor on a satellite ─────────────────────────────────────

LAUNCHCTL_SATELLITE = "PID\tStatus\tLabel\n311\t0\thaus.sanctum.vault-armor\n"


class _OkProvider(Provider):
    name = "ok"
    capabilities = 0  # type: ignore[assignment]

    def chat(self, _m, _o):  # type: ignore[no-untyped-def]
        yield ""

    def health(self) -> HealthSnapshot:
        return HealthSnapshot(ok=True, latency_ms=1, quota_remaining=None, detail=None)

    def cost(self, _u):  # type: ignore[no-untyped-def]
        from decimal import Decimal

        return Decimal(0)


def _launchctl(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if cmd[0].endswith("launchctl"):
        return subprocess.CompletedProcess(cmd, 0, stdout=LAUNCHCTL_SATELLITE, stderr="")
    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")


def _doctor(*args: str) -> Result:
    with (
        patch("sanctum_cli.commands.doctor.subprocess.run", side_effect=_launchctl),
        patch("sanctum_cli.commands.doctor.shutil.which", return_value="/usr/bin/launchctl"),
        patch("sanctum_cli.commands.doctor.make_provider", return_value=_OkProvider()),
        patch("sanctum_cli.commands.doctor._http_ok", return_value=False),
    ):
        return runner.invoke(app, ["doctor", *args])


def test_doctor_on_a_satellite_never_expects_or_suggests_hub_daemons(tmp_path, monkeypatch) -> None:
    _hub_looking_artifacts(tmp_path, monkeypatch)
    _instance(tmp_path, monkeypatch, SATELLITE)
    monkeypatch.setattr(su, "check_wave1", _must_not_run("the wave-1 check"))

    brief = _doctor()
    assert brief.exit_code == 0, brief.output
    assert "operational" in brief.stdout.lower()
    full = _doctor("--full")
    assert full.exit_code == 0, full.output
    assert "haus.sanctum.vault-armor" in full.stdout  # its own agents still show
    for text in (brief.stdout, full.stdout):
        assert "force-flow" not in text
        assert "Service principal" not in text
        assert "service-user install" not in text

    as_json = _doctor("--json")
    labels = [a["label"] for a in json.loads(as_json.stdout)["agents"]]
    assert labels == ["haus.sanctum.vault-armor"]


def test_doctor_on_a_declared_hub_expects_force_flow_and_wave1(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(su, "DAEMON_DIR", tmp_path / "no-daemons")
    _instance(tmp_path, monkeypatch, HUB)
    failing = su.Wave1Report(applicable=True, items=[su.CheckItem("sanctum user exists", False)])
    monkeypatch.setattr(su, "check_wave1", lambda **_k: failing)
    result = _doctor("--full")
    assert result.exit_code == 0
    out = result.stdout + (result.stderr or "")
    assert "haus.sanctum.force-flow" in out
    assert "run: sanctum service-user install" in out
