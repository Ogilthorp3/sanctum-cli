from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.commands.net import _firewalla_key_path
from tests.net import fixtures as fx

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


# ─── Firewalla SSH key resolution (discovery-first) ──────────────────


def test_firewalla_key_path_honors_instance_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured firewalla.ssh_key in instance.yaml wins over the default."""
    inst = tmp_path / "instance.yaml"
    custom = tmp_path / ".ssh" / "my_fw_key"
    inst.write_text(f"firewalla:\n  ssh_key: {custom}\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    assert _firewalla_key_path() == custom


def test_firewalla_key_path_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured key path with ~ is expanded to the user's home."""
    inst = tmp_path / "instance.yaml"
    inst.write_text("firewalla:\n  ssh_key: ~/.ssh/custom_fw\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    assert _firewalla_key_path() == Path("~/.ssh/custom_fw").expanduser()


def test_firewalla_key_path_defaults_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no firewalla.ssh_key set, the back-compat default applies."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(tmp_path / "absent.yaml"))
    assert _firewalla_key_path() == Path.home() / ".ssh" / "firewalla_ed25519"


def test_net_check_reports_double_nat() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.DOUBLE_NAT)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "Bell")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=True),
    ):
        result = runner.invoke(app, ["net", "check"])
    assert result.exit_code == 0, result.stdout
    assert "double" in result.stdout.lower()


def test_net_check_single_nat_says_optimal() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.SINGLE_NAT)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "Bell")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=True),
    ):
        result = runner.invoke(app, ["net", "check"])
    assert result.exit_code == 0
    assert "optimal" in result.stdout.lower() or "already" in result.stdout.lower()


def test_net_optimize_not_applicable_no_firewalla_exits_clean() -> None:
    with (
        patch(
            "sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.NO_FIREWALLA)
        ),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=False),
    ):
        result = runner.invoke(app, ["net", "optimize", "--yes"])
    assert result.exit_code == 0
    assert "nothing to optimize" in result.stdout.lower()


def test_net_optimize_double_nat_prints_plan() -> None:
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fx.FakeRunner(fx.DOUBLE_NAT)),
        patch("sanctum_cli.commands.net._build_http", return_value=fx.fake_http(200, "Bell")),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=True),
    ):
        result = runner.invoke(app, ["net", "optimize", "--yes", "--plan-only"])
    assert result.exit_code == 0, result.stdout
    assert "20:6d:31:51:67:82" in result.stdout
    assert "Advanced DMZ" in result.stdout


# ─── speedtest ───────────────────────────────────────────────────────

# A fake host runner: 2.5 GbE wired link, no Firewalla port data.
SPEEDTEST_WIRED: dict[tuple[str, ...], str] = {
    ("route",): "  interface: en7\n  gateway: 10.0.0.1\n",
    ("link_speed",): "\tmedia: autoselect (2500Base-T <full-duplex>)\n\tstatus: active\n",
    ("airport_ports",): "Hardware Port: Ethernet\nDevice: en7\n",
}


class _SpeedFakeRunner:
    def __init__(self, table: dict[tuple[str, ...], str]) -> None:
        self._table = table
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, tag: tuple[str, ...]) -> str:
        self.calls.append(tag)
        return self._table.get(tag, "")


def test_net_speedtest_no_test_json_is_parseable_and_skips_download() -> None:
    import json

    fake = _SpeedFakeRunner(SPEEDTEST_WIRED)
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fake),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=False),
    ):
        result = runner.invoke(app, ["net", "speedtest", "--no-test", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["multi_gbps"] is None  # no live test ran
    assert payload["single_gbps"] is None
    assert payload["ceiling_gbps"] == 2.5
    assert payload["on_wifi"] is False
    # The download tag must never be requested in --no-test mode.
    assert ("live_test",) not in fake.calls


def test_net_speedtest_no_test_human_output() -> None:
    fake = _SpeedFakeRunner(SPEEDTEST_WIRED)
    with (
        patch("sanctum_cli.commands.net._build_runner", return_value=fake),
        patch("sanctum_cli.commands.net._firewalla_present", return_value=False),
    ):
        result = runner.invoke(app, ["net", "speedtest", "--no-test"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "ceiling" in out
    assert "nat" in out
