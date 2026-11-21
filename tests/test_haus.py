"""``sanctum_cli.haus`` — the haus-only gate.

Detection must be cheap (no sockets, no Keychain value reads) and must NOT
break a command for an operator who HAS the haus. These tests inject
presence/absence per component and assert the gate either proceeds silently
(present) or banners + exits cleanly with ExitCode.OK (absent).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from sanctum_cli import cli, haus
from sanctum_cli.cli import app
from sanctum_cli.commands import council as council_cmd
from sanctum_cli.errors import ExitCode

runner = CliRunner()

# Captured before the autouse fixture below stubs it for every test.
_REAL_DEVICES_CANDIDATES = haus._devices_candidates

# These tests exercise the real detection + gate, so they must NOT get the
# autouse "haus present" stub from conftest.
pytestmark = pytest.mark.no_haus_stub

# All envs the gate inspects — cleared before each test so the host's real
# environment can't leak presence into a "absent" case.
_ALL_ENVS = (
    "SANCTUM_PROXYD_URL",
    "SANCTUM_COUNCIL_URL",
    "SANCTUM_PROXYD_CA",
    "SANCTUM_PROXYD_INSECURE",
    "SANCTUM_BRIDGE_URL",
    "SANCTUM_DEVICES_FILE",
    "SANCTUM_HAUS_ON_SATELLITE",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every filesystem + Keychain signal at an empty world by default."""
    for e in _ALL_ENVS:
        monkeypatch.delenv(e, raising=False)
    # No CA, no devices, no LaunchAgents, no bridge Keychain entry.
    monkeypatch.setattr(haus, "_CA_CERT", tmp_path / "absent-ca.crt")
    monkeypatch.setattr(
        haus, "_devices_candidates", lambda: (tmp_path / "a.yaml", tmp_path / "b.yaml")
    )
    monkeypatch.setattr(haus, "_launchagents_present", lambda: False)
    monkeypatch.setattr(haus.keychain, "exists", lambda *_a, **_k: False)


# ── absence → banner + clean exit ──────────────────────────────────────


@pytest.mark.parametrize("component", ["council", "bridge", "screen-time", "launchagents"])
def test_absent_banners_and_exits_ok(
    component: haus.Component, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as exc:
        haus.haus_required(component)
    assert exc.value.exit_code == int(ExitCode.OK)
    err = capsys.readouterr().err
    assert "full Sanctum haus" in err
    assert "sanctum.run" in err
    assert "beta" in err


def test_absent_exit_is_not_an_error_code() -> None:
    """The gate must exit 0, never a SanctumError code — it's 'not for you'."""
    with pytest.raises(typer.Exit) as exc:
        haus.haus_required("council")
    assert exc.value.exit_code == 0


# ── presence → proceed silently ────────────────────────────────────────


def test_council_present_via_ca(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("x", encoding="utf-8")
    monkeypatch.setattr(haus, "_CA_CERT", ca)
    assert haus.is_present("council") is True
    assert haus.haus_required("council") is None  # no raise


def test_council_present_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_PROXYD_URL", "https://127.0.0.1:4040")
    assert haus.is_present("council") is True
    assert haus.haus_required("council") is None


def test_bridge_present_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_BRIDGE_URL", "https://bridge.test")
    assert haus.is_present("bridge") is True


def test_bridge_present_via_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(haus.keychain, "exists", lambda *_a, **_k: True)
    assert haus.is_present("bridge") is True


def test_bridge_present_via_ca(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("x", encoding="utf-8")
    monkeypatch.setattr(haus, "_CA_CERT", ca)
    assert haus.is_present("bridge") is True


def test_screen_time_present_via_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dev = tmp_path / "devices.yaml"
    dev.write_text("family: {}\n", encoding="utf-8")
    monkeypatch.setattr(haus, "_devices_candidates", lambda: (dev,))
    assert haus.is_present("screen-time") is True
    assert haus.haus_required("screen-time") is None


def test_screen_time_presence_follows_the_current_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The candidates resolve per call, so conftest's fake home applies, never the real one."""
    monkeypatch.setattr(haus, "_devices_candidates", _REAL_DEVICES_CANDIDATES)
    assert haus.is_present("screen-time") is False
    devices = Path.home() / ".sanctum/screen-time/devices.yaml"
    devices.parent.mkdir(parents=True)
    devices.write_text("family: {}\n", encoding="utf-8")
    assert haus.is_present("screen-time") is True


def test_screen_time_present_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", "/anywhere/devices.yaml")
    assert haus.is_present("screen-time") is True


def test_launchagents_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(haus, "_launchagents_present", lambda: True)
    assert haus.is_present("launchagents") is True
    assert haus.haus_required("launchagents") is None


# ── detection is cheap: bridge never reads a Keychain VALUE ─────────────


def test_bridge_keychain_probe_is_existence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate must use keychain.exists (no value), never keychain.read."""
    calls: list[str] = []

    def _exists(account: str, service: str) -> bool:
        calls.append(f"exists:{service}")
        return False

    def _read(*_a: object, **_k: object) -> str:  # pragma: no cover - must not run
        calls.append("read")
        raise AssertionError("haus gate must not read a Keychain value")

    monkeypatch.setattr(haus.keychain, "exists", _exists)
    monkeypatch.setattr(haus.keychain, "read", _read)
    assert haus.is_present("bridge") is False
    assert calls == ["exists:sanctum-bridge-cf-access-client-id"]


# ── end-to-end through the real CLI ─────────────────────────────────────


def _force_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sanctum_cli.haus.is_present", lambda _c: False)


def _force_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sanctum_cli.haus.is_present", lambda _c: True)


@pytest.mark.parametrize(
    "argv",
    [
        ["brainstorm", "hello"],
        ["council", "hello"],
        ["chat", "hello"],
        ["code", "hello"],
        ["devices"],
        ["schedule"],
        ["bridge", "health"],
        ["agent", "list"],
        ["proxy", "status"],
        ["endocrine", "status"],
    ],
)
def test_cli_banners_without_haus(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Every gated command banners + exits 0 when the haus is absent."""
    _force_absent(monkeypatch)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert "full Sanctum haus" in result.output
    assert "sanctum.run" in result.output


def test_cli_proceeds_with_haus_then_fails_in_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the haus present the gate is transparent — devices runs its real body.

    No devices.yaml in this isolated env, so the *command body* (not the gate)
    exits 2 with its own message — proof the gate let it through.
    """
    _force_present(monkeypatch)
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(Path("/nonexistent/devices.yaml")))
    result = runner.invoke(app, ["devices"])
    assert "full Sanctum haus" not in result.output
    assert result.exit_code == 2
    assert "No devices.yaml" in result.output


def test_matrix_is_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """matrix is beta-safe eye-candy and must never banner, even with no haus."""
    _force_absent(monkeypatch)
    # Non-TTY in tests → matrix prints its polite refusal, not the haus banner.
    result = runner.invoke(app, ["matrix"])
    assert "full Sanctum haus" not in result.output


# ── a declared satellite is not the hub, whatever certificates it holds ─────────


def _satellite_with_every_hub_signal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A satellite as deployed: its own CA (for its local brain), plus every other signal."""
    instance = tmp_path / "instance.yaml"
    instance.write_text(
        "instance:\n  name: Second Haus\n  slug: second-haus\n  role: satellite\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(instance))
    ca = tmp_path / "ca.crt"
    ca.write_text("x", encoding="utf-8")
    monkeypatch.setattr(haus, "_CA_CERT", ca)
    monkeypatch.setattr(haus.keychain, "exists", lambda *_a, **_k: True)
    monkeypatch.setenv("SANCTUM_PROXYD_URL", "https://127.0.0.1:4040")
    monkeypatch.setenv("SANCTUM_BRIDGE_URL", "https://bridge.test")


@pytest.mark.parametrize("component", ["council", "bridge"])
def test_hub_services_are_absent_on_a_satellite(
    component: haus.Component, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    assert haus.is_present(component) is False


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
@pytest.mark.parametrize("component", ["council", "bridge"])
def test_the_override_restores_hub_detection_on_a_satellite(
    component: haus.Component, value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    monkeypatch.setenv(haus.SATELLITE_OVERRIDE_ENV, value)
    assert haus.is_present(component) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_only_a_truthy_override_counts(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    monkeypatch.setenv(haus.SATELLITE_OVERRIDE_ENV, value)
    assert haus.is_present("council") is False


def test_the_override_still_needs_the_component(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It lifts the satellite rule; it does not pretend a council is there."""
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    monkeypatch.setattr(haus, "_CA_CERT", tmp_path / "absent-ca.crt")
    monkeypatch.delenv("SANCTUM_PROXYD_URL")
    monkeypatch.setenv(haus.SATELLITE_OVERRIDE_ENV, "1")
    assert haus.is_present("council") is False


def test_a_satellites_own_devices_and_agents_still_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(tmp_path / "devices.yaml"))
    monkeypatch.setattr(haus, "_launchagents_present", lambda: True)
    assert haus.is_present("screen-time") is True
    assert haus.is_present("launchagents") is True


@pytest.mark.parametrize(
    "bad_line",
    ["since: 2026-02-30\n", "flag: !!bool maybe\n", "port: !!int ''\n"],
    ids=["impossible-date", "bool-maybe", "empty-int"],
)
def test_a_bad_value_elsewhere_does_not_hide_the_satellite(
    bad_line: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A value PyYAML cannot build elsewhere in instance.yaml used to let the hub services through."""
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    instance = tmp_path / "instance.yaml"
    instance.write_text(instance.read_text(encoding="utf-8") + bad_line, encoding="utf-8")
    assert haus.is_present("council") is False
    assert haus.is_present("bridge") is False

    def must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("bridge ran past the haus gate on a satellite")

    monkeypatch.setattr("sanctum_cli.commands.bridge.health_command", must_not_run)
    result = runner.invoke(app, ["bridge", "health"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "This machine is a satellite (instance.role: satellite)" in out


@pytest.mark.parametrize(
    "broken_line",
    ["\tcli: x\n", "cli: [unclosed\n", "cli: *nowhere\n", "---\nother: document\n"],
    ids=["tab-indent", "unclosed-list", "undefined-alias", "second-document"],
)
def test_a_syntax_error_after_the_role_does_not_hide_the_satellite(
    broken_line: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file that does not even compose used to read as roleless, so the satellite's CA passed."""
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    instance = tmp_path / "instance.yaml"
    instance.write_text(instance.read_text(encoding="utf-8") + broken_line, encoding="utf-8")
    assert haus.is_present("council") is False
    assert haus.is_present("bridge") is False

    def must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("bridge ran past the haus gate on a satellite")

    monkeypatch.setattr("sanctum_cli.commands.bridge.health_command", must_not_run)
    result = runner.invoke(app, ["bridge", "health"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "This machine is a satellite (instance.role: satellite)" in out


@pytest.mark.parametrize(
    "body",
    [
        "instance:\n  role: satellite  # \x1b[32mset by init\x1b[0m\n",
        "# \x1b[1mheader\x1b[0m\ninstance:\n  role: satellite\n",
    ],
    ids=["in-the-roles-comment", "in-a-comment-before-it"],
)
def test_escape_codes_in_a_comment_do_not_hide_the_satellite(
    body: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PyYAML refuses the file over the escape character, which the role reader reads as an
    unknown character: a comment holding one hides nothing.

    The role's line used to be dropped whole, and then everything after the first escape
    character, so the satellite read as roleless and its CA passed.
    """
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    instance = tmp_path / "instance.yaml"
    instance.write_text(body, encoding="utf-8")
    assert haus.is_present("council") is False
    assert haus.is_present("bridge") is False

    def must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("bridge ran past the haus gate on a satellite")

    monkeypatch.setattr("sanctum_cli.commands.bridge.health_command", must_not_run)
    result = runner.invoke(app, ["bridge", "health"])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "This machine is a satellite (instance.role: satellite)" in out


def test_a_declared_hub_with_the_ca_is_a_haus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    instance = tmp_path / "instance.yaml"
    instance.write_text(instance.read_text(encoding="utf-8").replace("satellite", "hub"), "utf-8")
    assert haus.is_present("council") is True
    assert haus.is_present("bridge") is True


@pytest.mark.parametrize(
    ("argv", "component", "body"),
    [
        (["council", "hello"], "council", "sanctum_cli.commands.council.council_ask"),
        (["chat", "hello"], "council", "sanctum_cli.commands.chat._resolve_prompt"),
        (["code", "hello"], "council", "sanctum_cli.commands.chat._resolve_prompt"),
        (["brainstorm", "hello"], "council", "sanctum_cli.commands.brainstorm._resolve_topic"),
        (
            ["endocrine", "status"],
            "council",
            "sanctum_cli.commands.endocrine_cmd._panel_or_neutral",
        ),
        (["bridge", "health"], "bridge", "sanctum_cli.commands.bridge.health_command"),
    ],
)
def test_hub_commands_banner_on_a_satellite(
    argv: list[str],
    component: str,
    body: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)

    def must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError(f"{argv[0]} ran past the haus gate on a satellite")

    monkeypatch.setattr(body, must_not_run)  # fail fast, never reach a live service
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "This command needs a full Sanctum haus" in out
    assert f"missing: {component}" in out
    assert (
        f"This machine is a satellite (instance.role: satellite) and the {component} is the "
        "hub's. To use it from here anyway, set SANCTUM_HAUS_ON_SATELLITE=1." in out
    )


@pytest.mark.parametrize(("override", "entered"), [(None, []), ("1", ["repl"])])
def test_bare_sanctum_in_a_terminal_opens_no_chamber_on_a_satellite(
    override: str | None,
    entered: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _satellite_with_every_hub_signal(monkeypatch, tmp_path)
    if override is not None:
        monkeypatch.setenv(haus.SATELLITE_OVERRIDE_ENV, override)
    calls: list[str] = []
    monkeypatch.setattr(cli.status, "status_command", lambda **_k: calls.append("status"))
    monkeypatch.setattr(cli, "_stdio_is_tty", lambda: True)
    monkeypatch.setattr(council_cmd, "_repl", lambda: calls.append("repl"))
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert calls == ["status", *entered]
