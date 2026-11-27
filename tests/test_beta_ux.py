"""Hermetic unit tests for ``sanctum bug-report``, ``sanctum ui``, and ``sanctum haus seats``."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import httpx
import yaml
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from sanctum_cli.cli import app
from sanctum_cli.commands import bug_report as bug_report_cmd
from sanctum_cli.commands import haus_cmd, ui_cmd

runner = CliRunner()


def test_bug_report_redact_text_scrubs_all_secrets_and_pii() -> None:
    sample = (
        "anthropic=sk-ant-api03-secretvalue1234567890 "
        "or=sk-or-v1-abcdef1234567890abcdef "
        "xai=xai-SuperSecretToken9876543210 "
        "gh=ghp_1234567890abcdefghij1234567890 "
        "google=AIzaSyA1234567890abcdefghijklmno "
        "Authorization: Bearer super_secret_bearer_token_value "
        "email=alice@example.org phone=+15145551234 "
        "home=/Users/alice/.sanctum/config.yaml "
        "lan=192.168.1.42 ts=100.64.0.42 host=hub.tail000000.ts.net "
        "loopback=127.0.0.1:4040"
    )
    cleaned = bug_report_cmd.redact_text(sample)
    for leaked in (
        "sk-ant-api03",
        "sk-or-v1-",
        "xai-SuperSecret",
        "ghp_123456",
        "AIzaSyA12345",
        "super_secret_bearer_token_value",
        "alice@example.org",
        "+15145551234",
        "/Users/alice",
        "192.168.1.42",
        "100.64.0.42",
        "hub.tail000000.ts.net",
    ):
        assert leaked not in cleaned, f"leaked {leaked} in {cleaned}"
    assert "127.0.0.1:4040" in cleaned


def test_bug_report_cli_json_and_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        haus_cmd,
        "probe_core_services",
        lambda: {"proxyd (:4040 TLS)": True, "sanctum-mlx (:1337 mTLS)": False},
    )
    res_json = runner.invoke(
        app,
        ["bug-report", "--skip-self-test", "--json", "--summary", "test at /Users/bob"],
    )
    assert res_json.exit_code == 0, res_json.output
    data = json.loads(res_json.output)
    assert data["services"]["proxyd (:4040 TLS)"] == "UP"
    assert "/Users/bob" not in data["summary"]
    assert "/Users/<redacted>" in data["summary"]

    res_md = runner.invoke(
        app,
        ["bug-report", "--skip-self-test", "--no-open", "--no-copy", "-m", "crash test"],
    )
    assert res_md.exit_code == 0, res_md.output
    assert "Sanctum Redacted Bug Report" in res_md.output
    assert "github.com/Ogilthorp3/sanctum-cli/issues/new" in res_md.output


def test_haus_resolve_client_cert_and_subscription_seats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 1. Direct client.crt layout
    c1 = tmp_path / "c1"
    c1.mkdir()
    (c1 / "client.crt").write_text("CERT")
    (c1 / "client.key").write_text("KEY")
    assert haus_cmd._resolve_client_cert(c1) == (c1 / "client.crt", c1 / "client.key")

    # 2. Per-service certs/clients/*.crt layout (operator hub)
    c2 = tmp_path / "c2"
    (c2 / "clients").mkdir(parents=True)
    (c2 / "clients" / "svc.crt").write_text("CERT")
    (c2 / "clients" / "svc.key").write_text("KEY")
    assert haus_cmd._resolve_client_cert(c2) == (
        c2 / "clients" / "svc.crt",
        c2 / "clients" / "svc.key",
    )

    # 3. Wire $0 subscription seats into portable proxyd config.yaml
    cfg_path = tmp_path / "config.yaml"
    haus_cmd.write_portable_proxyd_config(
        config_path=cfg_path, certs_dir=c1, api_model_name="Qwen3.8-27B-4bit"
    )
    assert haus_cmd.wire_subscription_seat_in_proxyd(config_path=cfg_path, seat="grok")
    assert haus_cmd.wire_subscription_seat_in_proxyd(config_path=cfg_path, seat="gemini")
    assert haus_cmd.wire_subscription_seat_in_proxyd(config_path=cfg_path, seat="claude")

    parsed = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    by_name = {m["name"]: m for m in parsed["models"]}
    assert by_name["council-finance"]["api_base"] == "http://127.0.0.1:4200"
    assert by_name["gemini-31-pro"]["api_base"] == "http://127.0.0.1:6543"
    assert by_name["council-max-thinking"]["api_base"] == "http://127.0.0.1:3456"
    assert parsed["fallbacks"]["council-finance"] == ["council-mlx"]
    assert "council-finance" not in parsed["model_aliases"]

    monkeypatch.setattr(
        haus_cmd,
        "probe_subscription_seats",
        lambda: {"grok-oauth": {"port": 4200, "seat": "council-finance", "up": True}},
    )
    res = runner.invoke(app, ["haus", "seats", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["grok-oauth"]["up"] is True


def test_ui_loopback_server_snapshot_and_csrf_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        haus_cmd,
        "probe_core_services",
        lambda: {"proxyd (:4040 TLS)": True, "sanctum-mlx (:1337 mTLS)": True},
    )
    monkeypatch.setattr(
        haus_cmd,
        "probe_subscription_seats",
        lambda: {"grok-oauth": {"port": 4200, "seat": "council-finance", "up": True}},
    )
    monkeypatch.setattr(ui_cmd, "_fetch_castellan_status", lambda: {"pool_mb": 51200, "used_mb": 20000, "headroom_mb": 31200})
    monkeypatch.setattr(ui_cmd, "dispatch_local_chat", lambda *, model, prompt: (True, f"echo:{model}:{prompt}", "proxyd :4040"))

    server, csrf = ui_cmd.create_ui_server(port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=5.0) as client:
            r_html = client.get(f"{base}/")
            assert r_html.status_code == 200
            assert "SANCTUM LOCAL CONTROL" in r_html.text
            assert r_html.headers.get("X-Frame-Options") == "DENY"

            r_snap = client.get(f"{base}/api/snapshot")
            assert r_snap.status_code == 200
            snap = r_snap.json()
            assert snap["services_up"] == 2
            assert snap["castellan"]["pool_mb"] == 51200

            # CSRF enforced on /api/bug-report and /api/chat
            assert client.get(f"{base}/api/bug-report").status_code == 403
            r_bug = client.get(f"{base}/api/bug-report", headers={"X-Sanctum-UI-Token": csrf})
            assert r_bug.status_code == 200
            assert "Sanctum" in r_bug.json()["markdown"] or "sanctum-cli" in r_bug.json()["markdown"]

            assert client.post(f"{base}/api/chat", json={"model": "council-mlx", "prompt": "hi"}).status_code == 403
            r_chat = client.post(
                f"{base}/api/chat",
                headers={"X-Sanctum-UI-Token": csrf},
                json={"model": "council-mlx", "prompt": "hi"},
            )
            assert r_chat.status_code == 200
            assert r_chat.json()["reply"] == "echo:council-mlx:hi"
    finally:
        server.shutdown()
        server.server_close()
