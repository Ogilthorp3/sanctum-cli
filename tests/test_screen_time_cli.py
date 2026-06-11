"""``sanctum screen-time`` — coverage view + phone-mode selection.

The honest-coverage surface that pairs with the screen-time engine's opt-in
network MAC-pause: it reads devices.yaml and tells the parent which of a kid's
personal devices are actually curfewed (hard-pause, Wi-Fi) vs deferred to Apple
Screen Time (presence-only). ``phone-mode`` flips a kid between Apple Screen
Time / Sanctum MAC-pause / both, previewing by default and writing only on
``--apply`` (with a .bak backup).

These tests pin the contract independently of the engine: expectations are
derived from the on-disk schema (family.<kid>.personal_devices + the
enforce_personal / per-device enforce flags), not shared with the producer.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import yaml
from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands import screen_time as st
from sanctum_cli.errors import UserError

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _config() -> dict:
    return {
        "family": {
            "kidA": {
                "role": "child",
                "personal_devices": [
                    {"name": "Kid A iPhone", "mac": "AA:BB:CC:00:00:01"},
                    {"name": "Kid A iPad", "mac": "AA:BB:CC:00:00:02"},
                ],
            },
            "kidB": {
                "role": "child",
                "personal_devices": [{"name": "Kid B iPhone", "mac": "AA:BB:CC:00:00:03"}],
            },
            "parent1": {
                "role": "parent",
                "personal_devices": [{"name": "Parent phone", "mac": "AA:BB:CC:00:00:09"}],
            },
        }
    }


# ── Pure opt-in predicate (mirrors the engine) ────────────────────────


class TestDeviceEnforced:
    def test_default_off(self) -> None:
        assert st._device_enforced({}, {"mac": "x"}) is False

    def test_child_level_macpause_on(self) -> None:
        assert st._device_enforced({"enforce_personal": "macpause"}, {"mac": "x"}) is True

    def test_per_device_optin(self) -> None:
        assert st._device_enforced({}, {"mac": "x", "enforce": "macpause"}) is True

    def test_per_device_off_overrides_child_on(self) -> None:
        member = {"enforce_personal": "macpause"}
        assert st._device_enforced(member, {"mac": "x", "enforce": "off"}) is False


# ── Coverage classification ───────────────────────────────────────────


class TestClassifyCoverage:
    def test_children_only_parents_excluded(self) -> None:
        rows = st.classify_coverage(_config())
        people = {r.person for r in rows}
        assert people == {"kidA", "kidB"}

    def test_default_all_presence_only(self) -> None:
        rows = st.classify_coverage(_config())
        assert all(r.klass == "presence-only" for r in rows)
        assert all(r.enforced is False for r in rows)

    def test_child_level_macpause_marks_hard_pause(self) -> None:
        cfg = _config()
        cfg["family"]["kidA"]["enforce_personal"] = "macpause"
        rows = st.classify_coverage(cfg)
        kida = [r for r in rows if r.person == "kidA"]
        kidb = [r for r in rows if r.person == "kidB"]
        assert all(r.klass == "hard-pause" and r.enforced for r in kida)
        assert all(r.klass == "presence-only" for r in kidb)  # untouched

    def test_per_device_optin_is_granular(self) -> None:
        cfg = _config()
        cfg["family"]["kidA"]["personal_devices"][0]["enforce"] = "macpause"
        rows = [r for r in st.classify_coverage(cfg) if r.person == "kidA"]
        enforced = {r.mac for r in rows if r.enforced}
        assert enforced == {"AA:BB:CC:00:00:01"}


# ── Phone-mode mutation (pure) ────────────────────────────────────────


class TestSetPhoneMode:
    def test_macpause_sets_flag(self) -> None:
        out = st.set_phone_mode(_config(), "kidA", "macpause")
        assert out["family"]["kidA"]["enforce_personal"] == "macpause"

    def test_both_sets_flag(self) -> None:
        out = st.set_phone_mode(_config(), "kidA", "both")
        assert out["family"]["kidA"]["enforce_personal"] == "macpause"

    def test_apple_clears_flag(self) -> None:
        cfg = _config()
        cfg["family"]["kidA"]["enforce_personal"] = "macpause"
        out = st.set_phone_mode(cfg, "kidA", "apple")
        assert out["family"]["kidA"].get("enforce_personal") in (None, "")

    def test_does_not_mutate_input(self) -> None:
        cfg = _config()
        snapshot = copy.deepcopy(cfg)
        st.set_phone_mode(cfg, "kidA", "macpause")
        assert cfg == snapshot

    def test_other_kids_untouched(self) -> None:
        out = st.set_phone_mode(_config(), "kidA", "macpause")
        assert "enforce_personal" not in out["family"]["kidB"]

    def test_unknown_mode_raises(self) -> None:
        try:
            st.set_phone_mode(_config(), "kidA", "bogus")
        except UserError:
            return
        raise AssertionError("expected UserError for unknown mode")

    def test_unknown_kid_raises(self) -> None:
        try:
            st.set_phone_mode(_config(), "nobody", "macpause")
        except UserError:
            return
        raise AssertionError("expected UserError for unknown kid")

    def test_parent_role_rejected(self) -> None:
        try:
            st.set_phone_mode(_config(), "parent1", "macpause")
        except UserError:
            return
        raise AssertionError("expected UserError when targeting a non-child")


# ── CLI integration ───────────────────────────────────────────────────


def _write_devices(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "devices.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_coverage_command_lists_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _write_devices(tmp_path, _config())
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(dev))
    result = runner.invoke(app, ["screen-time", "coverage"])
    assert result.exit_code == 0, result.stdout
    assert "kidA" in result.stdout
    assert "presence-only" in result.stdout


def test_phone_mode_preview_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _write_devices(tmp_path, _config())
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(dev))
    before = dev.read_text(encoding="utf-8")
    result = runner.invoke(app, ["screen-time", "phone-mode", "kidA", "macpause"])
    assert result.exit_code == 0, result.stdout
    assert dev.read_text(encoding="utf-8") == before  # preview only
    assert not (tmp_path / "devices.yaml.bak").exists()


def test_phone_mode_apply_writes_and_backs_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _write_devices(tmp_path, _config())
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(dev))
    result = runner.invoke(app, ["screen-time", "phone-mode", "kidA", "macpause", "--apply"])
    assert result.exit_code == 0, result.stdout
    written = yaml.safe_load(dev.read_text(encoding="utf-8"))
    assert written["family"]["kidA"]["enforce_personal"] == "macpause"
    assert (tmp_path / "devices.yaml.bak").exists()  # backup made


def test_phone_mode_unknown_kid_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _write_devices(tmp_path, _config())
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(dev))
    result = runner.invoke(app, ["screen-time", "phone-mode", "ghost", "macpause", "--apply"])
    assert result.exit_code != 0
    assert dev.read_text(encoding="utf-8") == yaml.safe_dump(_config())  # unchanged


def test_coverage_renders_user_strings_literally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hostile input at the boundary: a MAC whose octets spell a Rich emoji
    # shortcode (:cd:) and a device name carrying markup must render verbatim,
    # not as 💿 or parsed tags.
    cfg = {
        "family": {
            "kidA": {
                "role": "child",
                "personal_devices": [
                    {"name": "[red]Pwn[/] phone", "mac": "7A:87:AC:CD:8E:A2"}
                ],
            }
        }
    }
    dev = _write_devices(tmp_path, cfg)
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(dev))
    result = runner.invoke(app, ["screen-time", "coverage"])
    assert result.exit_code == 0, result.stdout
    assert "💿" not in result.stdout  # MAC not mangled into an emoji
    assert "AC:CD:8E" in result.stdout  # MAC octets intact
    assert "[red]" in result.stdout  # device name not parsed as markup
