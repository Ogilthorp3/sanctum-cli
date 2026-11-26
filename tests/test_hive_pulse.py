"""Hermetic unit tests for sanctum_cli.hive_pulse and `sanctum hive`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
from typer.testing import CliRunner

from sanctum_cli import hive_pulse
from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _make_mock_transport(captured: list[tuple[str, str, dict | None]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        captured.append((request.method, str(request.url), body))
        if request.method == "POST" and request.url.path == "/pulse":
            return httpx.Response(200, json={"ok": True, "hive_nodes": 7, "active_7d": 5})
        if request.method == "DELETE" and request.url.path.startswith("/pulse/"):
            return httpx.Response(200, json={"ok": True, "removed": True})
        if request.method == "GET" and request.url.path == "/latest.json":
            return httpx.Response(
                200,
                json={
                    "latest_version": "1.2.0",
                    "min_supported": "1.0.0",
                    "release_url": "https://github.com/Ogilthorp3/sanctum-cli/releases/latest",
                },
            )
        if request.method == "GET" and request.url.path == "/analytics":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "total_nodes": 7,
                    "onboard_completed": 6,
                    "active_24h": 4,
                    "active_7d": 5,
                    "active_30d": 7,
                    "total_ram_gb": 512,
                    "version_checks_24h": 12,
                    "version_checks_7d": 40,
                    "by_chip": {"Apple M4 Pro": 4, "Apple M1": 3},
                    "by_country": {"CA": 5, "US": 2},
                },
            )
        return httpx.Response(404, json={"ok": False})

    return httpx.MockTransport(handler)


def test_opt_in_join_heartbeat_throttle_and_leave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "hive-pulse.json"
    monkeypatch.setenv(hive_pulse.ENV_HIVE_STATE, str(state_file))
    monkeypatch.setenv(hive_pulse.ENV_HIVE_URL, "https://hive.test")

    captured: list[tuple[str, str, dict | None]] = []
    transport = _make_mock_transport(captured)

    # Strictly NO-OP before explicit opt-in
    assert hive_pulse.maybe_heartbeat("status", transport=transport) is False
    assert captured == []

    # Opt-in join sends anonymous payload (no hostname/user/ip)
    res = hive_pulse.join_hive(event="onboard", transport=transport)
    assert res["ok"] is True
    assert res["hive_nodes"] == 7
    assert len(captured) == 1
    method, url, payload = captured[0]
    assert method == "POST"
    assert url == "https://hive.test/pulse"
    assert payload is not None
    assert payload["event"] == "onboard"
    assert payload["onboard_completed"] is True
    assert 16 <= len(payload["node_id"]) <= 64
    assert "hostname" not in payload
    assert "ip" not in payload

    # Second heartbeat within 24h is throttled
    assert hive_pulse.maybe_heartbeat("status", min_interval_hours=24.0, transport=transport) is False
    assert len(captured) == 1

    # Forced/expired interval sends heartbeat
    assert hive_pulse.maybe_heartbeat("backup", min_interval_hours=0.0, transport=transport) is True
    assert len(captured) == 2
    assert captured[1][2] is not None
    assert captured[1][2]["event"] == "backup"

    # Leave deletes remote record and disables local state
    left = hive_pulse.leave_hive(transport=transport)
    assert left["ok"] is True
    assert left["removed"] is True
    assert captured[2][0] == "DELETE"
    assert hive_pulse.load_state()["enabled"] is False
    assert hive_pulse.maybe_heartbeat("status", min_interval_hours=0.0, transport=transport) is False


def test_update_check_respects_opt_out_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "hive-pulse.json"
    monkeypatch.setenv(hive_pulse.ENV_HIVE_STATE, str(state_file))
    monkeypatch.setenv(hive_pulse.ENV_HIVE_URL, "https://hive.test")

    captured: list[tuple[str, str, dict | None]] = []
    transport = _make_mock_transport(captured)

    # Opt-out env var suppresses network call
    monkeypatch.setenv(hive_pulse.ENV_NO_UPDATE_CHECK, "1")
    assert hive_pulse.maybe_check_update(current_version="1.0.1", transport=transport) is None
    assert captured == []

    # Re-enable: detects 1.2.0 > 1.0.1 and caches for 24h
    monkeypatch.delenv(hive_pulse.ENV_NO_UPDATE_CHECK)
    newer = hive_pulse.maybe_check_update(current_version="1.0.1", transport=transport)
    assert newer == "1.2.0"
    assert len(captured) == 1
    assert "node_id" not in captured[0][1]

    # Cached response does not hit network again within 24h
    newer2 = hive_pulse.maybe_check_update(current_version="1.0.1", transport=transport)
    assert newer2 == "1.2.0"
    assert len(captured) == 1

    # Already on latest returns None
    assert hive_pulse.maybe_check_update(current_version="1.2.0", transport=transport) is None


def test_cli_hive_status_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hive_pulse.ENV_HIVE_STATE, str(tmp_path / "hive-pulse.json"))
    res = runner.invoke(app, ["hive", "status", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["enabled"] is False
    assert "payload_preview" in data
