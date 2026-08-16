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

from sanctum_cli import haus
from sanctum_cli.cli import app
from sanctum_cli.errors import ExitCode

runner = CliRunner()

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
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every filesystem + Keychain signal at an empty world by default."""
    for e in _ALL_ENVS:
        monkeypatch.delenv(e, raising=False)
    # No CA, no devices, no LaunchAgents, no bridge Keychain entry.
    monkeypatch.setattr(haus, "_CA_CERT", tmp_path / "absent-ca.crt")
    monkeypatch.setattr(haus, "_DEVICES_CANDIDATES", (tmp_path / "a.yaml", tmp_path / "b.yaml"))
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
    monkeypatch.setattr(haus, "_DEVICES_CANDIDATES", (dev,))
    assert haus.is_present("screen-time") is True
    assert haus.haus_required("screen-time") is None


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
