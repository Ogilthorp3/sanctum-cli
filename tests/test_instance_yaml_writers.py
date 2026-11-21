"""The raw instance.yaml readers and writers in onboard, setup, mesh and the cloud setup
wizards read through ``config.load_raw``: a file PyYAML cannot read or build (an impossible
date, a bad tag, a list at the top, a syntax error) is a clean ``ConfigError``. It never
escapes as a traceback, and it is never "repaired" by rewriting the file with only the new
keys. The Setup Assistant's family pane reads screen-time's devices.yaml the same way.
"""

from __future__ import annotations

import os
import stat
import types
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from sanctum_cli import config
from sanctum_cli.backends import b2, gdrive, r2
from sanctum_cli.commands import mesh, onboard, setup
from sanctum_cli.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_HEAD = "instance:\n  name: Box\n  slug: box\n"
BROKEN = {
    "impossible-date": (_HEAD + "since: 2026-02-30\n", "2026-02-30 on line 4 is not a valid date"),
    "bad-tag": (_HEAD + "flag: !!bool maybe\n", "!!bool maybe on line 4 is not a valid true/false"),
    "list-at-the-top": ("- keep\n- me\n", "top-level YAML must be a mapping"),
    "syntax-error": (_HEAD + "cli: [unclosed\n", "YAML parse error"),
}


def _broken(case: str) -> tuple[Path, str, str]:
    """The test's instance.yaml (``SANCTUM_INSTANCE_FILE``) holding a broken body."""
    body, pointer = BROKEN[case]
    target = config.instance_path()
    target.write_text(body, encoding="utf-8")
    return target, body, pointer


def _untouched(target: Path, body: str) -> None:
    assert target.read_text(encoding="utf-8") == body
    assert not target.with_name(target.name + ".bak").exists()


_WRITERS: dict[str, Callable[[Path], None]] = {
    "identity": lambda p: onboard.set_instance_identity("Alex", "+15555550100", path=p),
    "providers": lambda p: onboard.set_provider_config(gemini={"model": "x"}, path=p),
    "firewalla": lambda p: onboard.set_firewalla_bridge(
        token="t", device_ip="", device_mac="", port=1984, path=p, token_file=p.with_name("tok")
    ),
    "ha-green": lambda p: onboard.set_ha_green(
        token="t",
        host="h",
        port=8123,
        device_mac="m",
        tailnet_node="n",
        path=p,
        token_file=p.with_name("tok"),
    ),
    "device": lambda p: onboard.set_device_reference(
        kind="router", brand="b", host="h", keychain_service="s", keychain_account="a", path=p
    ),
}


@pytest.mark.parametrize("case", sorted(BROKEN))
@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_onboard_writers_report_and_never_rewrite(case: str, writer: str) -> None:
    target, body, pointer = _broken(case)
    with pytest.raises(ConfigError) as refused:
        _WRITERS[writer](target)
    assert pointer in refused.value.message
    _untouched(target, body)
    assert not target.with_name("tok").exists()  # no token written either


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_onboard_readers_fall_back(case: str) -> None:
    target, _body, _pointer = _broken(case)
    assert onboard._preferred_name("login-name") == "login-name"
    assert onboard._identity_configured(target) is False


@pytest.mark.parametrize("case", sorted(BROKEN))
@pytest.mark.parametrize(
    ("key", "write"),
    [
        ("mesh.label", lambda: mesh._persist_mesh_config("haus", "100.64.0.1")),
        (
            "mesh.champion",
            lambda: mesh._record_champion(
                types.SimpleNamespace(  # type: ignore[arg-type]
                    content_hash="sha256:abc", manifest=types.SimpleNamespace(base_model="m")
                )
            ),
        ),
    ],
)
def test_mesh_writes_skip_with_a_note_and_never_rewrite(
    case: str, key: str, write: Callable[[], None], capsys: pytest.CaptureFixture[str]
) -> None:
    """Best-effort: the command goes on, the file keeps every block, and stderr says why."""
    target, body, pointer = _broken(case)
    write()
    _untouched(target, body)
    err = " ".join(capsys.readouterr().err.split())
    assert err.startswith(f"note: {key} was not saved: ")
    assert pointer in err


def test_mesh_write_still_creates_a_missing_file() -> None:
    target = config.instance_path()
    mesh._persist_mesh_config("haus", None)
    assert config.load_raw(target) == {"mesh": {"label": "haus"}}


# ── setup (the browser Setup Assistant): an error reply, never a crashed handler ──


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_setup_backup_pane_never_rewrites(case: str) -> None:
    """It used to swallow the parse error and save a file holding only the policy."""
    target, body, pointer = _broken(case)
    res = setup.do_action("backup", "save", {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12})
    assert res["ok"] is False
    assert res["detail"].startswith("nothing saved: ")
    assert pointer in res["detail"]
    _untouched(target, body)


def _paired_firewalla(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.commands import screen_time
    from sanctum_cli.devices import firewalla

    monkeypatch.setattr(firewalla, "_bridge_url", lambda: "http://127.0.0.1:1984")
    monkeypatch.setattr(
        screen_time,
        "validate_firewalla_pairing",
        lambda url, token, **k: screen_time.PairingResult("paired", True, "ok"),
    )


def test_setup_firewalla_pane_reports_an_unreadable_file(monkeypatch: pytest.MonkeyPatch) -> None:
    target, body, pointer = _broken("impossible-date")
    _paired_firewalla(monkeypatch)
    res = setup.do_action("firewalla", "save", {"url": "http://127.0.0.1:1984", "token": "t"})
    assert res["ok"] is False
    assert res["detail"].startswith("not paired — ")
    assert pointer in res["detail"]
    _untouched(target, body)


def test_setup_ha_pane_reports_an_unreadable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pairing verifies, but nothing is saved: not the services block, not the token."""
    from sanctum_cli.devices import ha_green

    target, body, pointer = _broken("impossible-date")
    token_file = tmp_path / "ha-token"
    monkeypatch.setattr(ha_green, "api_running", lambda **_k: True)
    monkeypatch.setattr(ha_green, "_HA_TOKEN_FILE", token_file)
    res = setup.do_action(
        "ha", "save", {"url": "http://homeassistant.local:8123", "token": "owner-token"}
    )
    assert res["ok"] is False
    assert res["detail"].startswith("not saved — ")
    assert pointer in res["detail"]
    _untouched(target, body)
    assert not token_file.exists()


def test_setup_provider_pane_stores_nothing_on_an_unreadable_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, body, pointer = _broken("impossible-date")
    stored: list[Any] = []
    monkeypatch.setattr(onboard, "store_device_secret", lambda **k: stored.append(k))
    monkeypatch.setattr(onboard, "_provider_health", lambda *_a: pytest.fail("no probe"))
    res = setup.do_action("provider", "save", {"kind": "gemini", "key": "k"})
    assert res["ok"] is False
    assert res["detail"].startswith("nothing stored: ")
    assert pointer in res["detail"]
    assert stored == []
    _untouched(target, body)


def test_setup_provider_pane_keeps_the_verified_key_when_routing_cannot_be_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file breaks between the probe and the write: say exactly what happened."""
    monkeypatch.setattr(onboard, "_config_with_provider_overrides", lambda **_k: object())
    monkeypatch.setattr(onboard, "store_device_secret", lambda **_k: None)
    healthy = types.SimpleNamespace(ok=True, detail=None)
    monkeypatch.setattr(onboard, "_provider_health", lambda *_a: healthy)
    revoked: list[Any] = []
    monkeypatch.setattr(onboard, "_revoke_device_secret", lambda **k: revoked.append(k))
    target, body, pointer = _broken("bad-tag")
    res = setup.do_action("provider", "save", {"kind": "claude", "key": "k"})
    assert res["ok"] is False
    assert res["detail"].startswith("the key works and is stored, but routing was not saved: ")
    assert pointer in res["detail"]
    assert revoked == []
    _untouched(target, body)


# ── cloud setup wizards: the persist step that ends each wizard ──

_PERSIST: dict[str, Callable[[Path], None]] = {
    "r2": lambda p: r2._persist(
        p,
        slot="primary",
        repo="s3:https://r2.test/b",
        keychain_service_restic="s",
        keychain_account="a",
    ),
    "b2": lambda p: b2._persist_to_instance_yaml(
        p, bucket="b", keychain_service_restic="s", keychain_account="a"
    ),
    "gdrive-primary": lambda p: gdrive._persist_gdrive(
        target_slot="primary",
        instance_path=p,
        repo="rclone:gdrive-sanctum:x",
        keychain_service_restic="s",
        keychain_account="a",
    ),
    "gdrive-secondary": lambda p: gdrive._persist_gdrive(
        target_slot="secondary",
        instance_path=p,
        repo="rclone:gdrive-sanctum:x",
        keychain_service_restic="s",
        keychain_account="a",
    ),
}


@pytest.mark.parametrize("case", sorted(BROKEN))
@pytest.mark.parametrize("persist", sorted(_PERSIST))
def test_cloud_setup_persist_reports_and_never_rewrites(case: str, persist: str) -> None:
    """An impossible date was a ValueError traceback at the very end of the wizard."""
    target, body, pointer = _broken(case)
    with pytest.raises(ConfigError) as refused:
        _PERSIST[persist](target)
    assert pointer in refused.value.message
    assert target.read_text(encoding="utf-8") == body
    assert list(target.parent.glob(target.name + ".*")) == []  # no .bak.<stamp>, no .tmp


@pytest.mark.parametrize("persist", sorted(_PERSIST))
def test_cloud_setup_persist_reports_a_file_that_is_not_utf8(persist: str) -> None:
    target = config.instance_path()
    before = "instance:\n  name: Café\n  slug: cafe\n".encode("latin-1")
    target.write_bytes(before)
    with pytest.raises(ConfigError, match="is not UTF-8 text"):
        _PERSIST[persist](target)
    assert target.read_bytes() == before
    assert list(target.parent.glob(target.name + ".*")) == []


_NOT_A_BLOCK = {
    "a-string-cli": ("cli: just text\n", "cli"),
    "a-list-cloud-backup": ("cli:\n  cloud_backup:\n    - b2:old-bucket\n", "cli.cloud_backup"),
    "an-empty-list-cli": ("cli: []\n", "cli"),
    "an-empty-string-cli": ("cli: ''\n", "cli"),
    "a-false-cloud-backup": ("cli:\n  cloud_backup: false\n", "cli.cloud_backup"),
}


@pytest.mark.parametrize("case", sorted(_NOT_A_BLOCK))
@pytest.mark.parametrize("persist", sorted(_PERSIST))
def test_cloud_setup_persist_refuses_a_cli_entry_that_is_not_a_block_of_keys(
    case: str, persist: str
) -> None:
    """In 0.15.5 an empty list, an empty string or false was silently replaced, a string cli
    was an AttributeError traceback (b2 and the gdrive primary slot: a bare LocalError), and a
    list cloud_backup a TypeError traceback."""
    tail, name = _NOT_A_BLOCK[case]
    target = config.instance_path()
    target.write_text(_HEAD + tail, encoding="utf-8")
    with pytest.raises(ConfigError) as refused:
        _PERSIST[persist](target)
    error = refused.value
    assert error.message == f"{name} in {target} is not a block of keys; the file was not changed"
    assert error.fix is not None
    fix = error.fix.splitlines()
    assert fix[0] == f"edit {target} by hand so that {name} is a block of keys, like this:"
    assert fix[1:3] == ["    cli:", "      cloud_backup:"]
    assert any(line.startswith("          repo: ") for line in fix)
    assert target.read_text(encoding="utf-8") == _HEAD + tail
    assert list(target.parent.glob(target.name + ".*")) == []


@pytest.mark.parametrize(
    "tail",
    [
        "cli:\n",
        "cli: ~\n",
        "cli:\n  cloud_backup: ~\n",
        "cli: {}\n",
        "cli:\n  cloud_backup: {}\n",
    ],
    ids=["cli", "cli-tilde", "cloud-backup", "cli-braces", "cloud-backup-braces"],
)
@pytest.mark.parametrize("persist", sorted(_PERSIST))
def test_cloud_setup_persist_fills_an_empty_entry(tail: str, persist: str) -> None:
    """A ``cli`` or ``cloud_backup`` key with no value counts as absent, and ``{}`` is an
    empty block of keys: the new slot goes in."""
    target = config.instance_path()
    target.write_text(_HEAD + tail, encoding="utf-8")
    _PERSIST[persist](target)
    config.load(target)  # the result passes the schema
    cloud_backup = config.load_raw(target)["cli"]["cloud_backup"]
    slot = "secondary" if persist == "gdrive-secondary" else "primary"
    assert sorted(cloud_backup) == sorted([slot, "retention"])


class _PreflightReachedError(Exception):
    """Raised in place of a wizard's pre-flight: its start check let the file through."""


@pytest.mark.parametrize("tail", ["cli:\n", "cli: ~\n"], ids=["no-value", "tilde"])
@pytest.mark.parametrize("wizard", [r2, b2, gdrive], ids=["r2", "b2", "gdrive"])
def test_every_cloud_wizard_starts_on_an_empty_cli_block(
    wizard: types.ModuleType, tail: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each wizard loads the file against the schema first; an empty ``cli:`` was refused
    there (exit 5) although the persist step at its end fills one."""
    config.instance_path().write_text(_HEAD + tail, encoding="utf-8")

    def reached(*_args: object) -> None:
        raise _PreflightReachedError

    monkeypatch.setattr(wizard, "_preflight", reached)
    with pytest.raises(_PreflightReachedError):
        wizard.run_wizard(auto_open=False, persist=True)


@pytest.mark.parametrize("persist", sorted(_PERSIST))
def test_cloud_setup_persist_keeps_the_original_bytes_its_mode_and_a_symlink(
    persist: str, tmp_path: Path
) -> None:
    """The backup was re-encoded text (CRLF lost) with default permissions, and the new file
    replaced a symlinked instance.yaml with a plain one, leaving the link's target stale."""
    real = tmp_path / "config-repo" / "instance.yaml"
    real.parent.mkdir()
    original = b"# my haus\r\ninstance:\r\n  name: Box\r\n  slug: box\r\n"
    real.write_bytes(original)
    real.chmod(0o600)
    link = tmp_path / "instance.yaml"
    link.symlink_to(real)

    _PERSIST[persist](link)

    assert link.is_symlink()
    assert link.resolve() == real.resolve()
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert config.load(real).cli.cloud_backup is not None
    (backup,) = tmp_path.glob("instance.yaml.bak.*")
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.parametrize("persist", sorted(_PERSIST))
def test_cloud_setup_persist_leaves_a_read_only_file_alone(persist: str) -> None:
    target = config.instance_path()
    target.write_text(_HEAD, encoding="utf-8")
    target.chmod(0o444)
    with pytest.raises(ConfigError, match="is read-only; the file was not changed"):
        _PERSIST[persist](target)
    assert target.read_text(encoding="utf-8") == _HEAD
    assert list(target.parent.glob(target.name + ".*")) == []


# ── setup's family pane: screen-time's devices.yaml ──

_DEVICES_BROKEN = {
    "impossible-date": (
        "family: {}\nsince: 2026-02-30\n",
        "2026-02-30 on line 2 is not a valid date",
    ),
    "bad-tag": (
        "family: {}\nflag: !!bool maybe\n",
        "!!bool maybe on line 2 is not a valid true/false",
    ),
    "syntax-error": ("family: [unclosed\n", "YAML parse error"),
    "list-at-the-top": ("- keep\n- me\n", "is a list, not keys such as family:"),
    "single-value": ("just text\n", "is a single value, not keys such as family:"),
}


@pytest.mark.parametrize("case", sorted(_DEVICES_BROKEN))
def test_setup_family_pane_reports_and_never_rewrites(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bad value was a traceback in the pane's handler; a list at the top was replaced."""
    body, pointer = _DEVICES_BROKEN[case]
    devices = tmp_path / "devices.yaml"
    devices.write_text(body, encoding="utf-8")
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))
    res = setup.do_action("family", "save", {"members": [{"name": "Kid", "devices": []}]})
    assert res["ok"] is False
    assert res["detail"].startswith("nothing saved: ")
    assert pointer in res["detail"]
    assert devices.read_text(encoding="utf-8") == body
    assert not (tmp_path / "devices.yaml.bak").exists()


def test_setup_family_pane_reports_a_devices_file_that_is_not_utf8(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    devices = tmp_path / "devices.yaml"
    before = "family:\n  kid:\n    name: Zoé\n".encode("latin-1")
    devices.write_bytes(before)
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))
    res = setup.do_action("family", "save", {"members": [{"name": "Kid", "devices": []}]})
    assert res["ok"] is False
    assert f"nothing saved: {devices} is not UTF-8 text" in res["detail"]
    assert devices.read_bytes() == before


def test_setup_family_pane_still_fills_an_empty_devices_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    devices = tmp_path / "devices.yaml"
    devices.write_text("# family goes here\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(devices))
    res = setup.do_action("family", "save", {"members": [{"name": "Kid", "devices": []}]})
    assert res["ok"] is True, res
    assert yaml.safe_load(devices.read_text(encoding="utf-8")) == {
        "family": {"kid": {"name": "Kid", "role": "child", "personal_devices": []}}
    }
