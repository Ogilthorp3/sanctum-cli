"""Hermetic unit tests for ``sanctum dragon``, ``sanctum yoda``, and unified ``sanctum update``."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from sanctum_cli.cli import app
from sanctum_cli.commands import dragon as dragon_cmd
from sanctum_cli.commands import haus_cmd
from sanctum_cli.commands import update as update_cmd
from sanctum_cli.commands import yoda as yoda_cmd

runner = CliRunner()


def _make_dragon_zip_bytes(tag: str = "dragon-2") -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("sanctum-dragon-guest/chat.py", "# chat stub\n")
        zf.writestr("sanctum-dragon-guest/serve.py", "# serve stub\n")
        zf.writestr(
            "sanctum-dragon-guest/adapter/adapter_config.json",
            json.dumps({"release_tag": tag, "num_layers": 64}),
        )
        zf.writestr("sanctum-dragon-guest/adapter/adapters.safetensors", b"FAKE_TENSORS")
    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def test_dragon_resolve_tier_auto() -> None:
    assert dragon_cmd.resolve_tier("auto", ram_gb=64) == ("27b", dragon_cmd.MODEL_27B)
    assert dragon_cmd.resolve_tier("auto", ram_gb=32) == ("27b", dragon_cmd.MODEL_27B)
    assert dragon_cmd.resolve_tier("auto", ram_gb=16) == ("9b", dragon_cmd.MODEL_9B)
    assert dragon_cmd.resolve_tier("9b", ram_gb=64) == ("9b", dragon_cmd.MODEL_9B)
    assert dragon_cmd.resolve_tier("27b", ram_gb=16) == ("27b", dragon_cmd.MODEL_27B)


def test_dragon_download_verify_and_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    d_dir = tmp_path / "dragon"
    monkeypatch.setenv("SANCTUM_DRAGON_DIR", str(d_dir))
    monkeypatch.setenv("SANCTUM_HIVE_PULSE_URL", "https://hive.test")

    zip_bytes, sha = _make_dragon_zip_bytes("dragon-2")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/latest.json":
            return httpx.Response(
                200,
                json={
                    "latest_version": "1.0.2",
                    "dragon_tag": "dragon-2",
                    "dragon_adapter": "qwen-haus-dragon-193d2d7407ec",
                    "dragon_url": "https://hive.test/dragon-2.zip",
                    "dragon_sha256": sha,
                    "mlx_lm_version": "0.32.2",
                },
            )
        if request.url.path == "/dragon-2.zip":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(dragon_cmd, "_ensure_venv", lambda target, ver: target / ".venv/bin/python")
    monkeypatch.setattr(dragon_cmd, "_dragon_health", lambda port=8787: None)

    # Reject wrong sha256 fail-closed
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        dragon_cmd._download_and_verify_bundle(
            "https://hive.test/dragon-2.zip",
            "0" * 64,
            d_dir,
            transport=transport,
        )

    # Valid sha256 stages bundle
    got_sha = dragon_cmd._download_and_verify_bundle(
        "https://hive.test/dragon-2.zip",
        sha,
        d_dir,
        transport=transport,
    )
    assert got_sha == sha
    assert (d_dir / "chat.py").is_file()
    assert (d_dir / "serve.py").is_file()
    assert (d_dir / "adapter" / "adapters.safetensors").is_file()

    # Save older manifest and verify update_dragon_if_needed upgrades to dragon-2
    dragon_cmd.save_manifest(
        {
            "release_tag": "dragon-1",
            "zip_sha256": "oldsha",
            "mlx_lm_version": "0.31.3",
        }
    )
    ok_dry, msg_dry = dragon_cmd.update_dragon_if_needed(dry_run=True, transport=transport)
    assert ok_dry
    assert "dragon-1 -> dragon-2" in msg_dry

    ok_apply, msg_apply = dragon_cmd.update_dragon_if_needed(dry_run=False, transport=transport)
    assert ok_apply, msg_apply
    mf = dragon_cmd.load_manifest()
    assert mf["release_tag"] == "dragon-2"
    assert mf["zip_sha256"] == sha
    assert mf["mlx_lm_version"] == "0.32.2"

    # Second run is already up-to-date
    ok_again, msg_again = dragon_cmd.update_dragon_if_needed(dry_run=False, transport=transport)
    assert ok_again
    assert "already up-to-date" in msg_again


def test_dragon_safe_extract_blocks_zipslip(tmp_path: Path) -> None:
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../escape.txt", "nope")
    with pytest.raises(ValueError, match="unsafe zip entry"):
        dragon_cmd._safe_extract_zip(bad_zip, tmp_path / "out")


def test_yoda_normalize_e164() -> None:
    assert yoda_cmd.normalize_e164("+1 (555) 123-4567") == "+15551234567"
    assert yoda_cmd.normalize_e164("555-123-4567") == "+15551234567"
    assert yoda_cmd.normalize_e164("+33 6 12 34 56 78") == "+33612345678"
    assert yoda_cmd.normalize_e164("not-a-number") is None


def test_yoda_setup_dry_run_and_operator_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_YODA_STATE", str(tmp_path / "yoda.json"))
    monkeypatch.setattr(yoda_cmd, "is_operator_haus", lambda: True)
    res_guard = runner.invoke(app, ["yoda", "setup", "--dry-run"])
    assert res_guard.exit_code == 0
    assert "operator haus" in res_guard.output.lower()

    monkeypatch.setattr(yoda_cmd, "is_operator_haus", lambda: False)
    res_dry = runner.invoke(app, ["yoda", "setup", "--dry-run", "--mode", "qr"])
    assert res_dry.exit_code == 0
    assert "signal-cli" in res_dry.output.lower()
    assert "sanctum-yoda" in res_dry.output


def test_yoda_status_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SANCTUM_YODA_STATE", str(tmp_path / "yoda.json"))
    monkeypatch.setenv("SANCTUM_DRAGON_DIR", str(tmp_path / "dragon"))
    monkeypatch.setattr(yoda_cmd, "is_operator_haus", lambda: False)
    monkeypatch.setattr(yoda_cmd, "_cmd_version", lambda cmd: "0.14.10" if cmd[0] == "signal-cli" else "2026.11.10")
    monkeypatch.setattr(dragon_cmd, "_dragon_health", lambda port=8787: None)
    monkeypatch.setattr(haus_cmd, "proxyd_health", lambda: False)
    monkeypatch.setattr(haus_cmd, "mlx_health", lambda: False)

    res = runner.invoke(app, ["yoda", "status", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["installed"] is False
    assert data["signal_cli"] == "0.14.10"
    assert data["openclaw"] == "2026.11.10"
    assert data["proxyd_running"] is False
    assert data["sanctum_mlx_running"] is False


def test_unified_update_runs_dragon_and_yoda_when_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANCTUM_DRAGON_DIR", str(tmp_path / "dragon"))
    monkeypatch.setenv("SANCTUM_YODA_STATE", str(tmp_path / "yoda.json"))
    dragon_cmd.save_manifest({"release_tag": "dragon-1", "zip_sha256": "abc", "mlx_lm_version": "0.31.3"})
    yoda_cmd.save_yoda_state({"installed": True, "profile": "sanctum-yoda"})

    called: list[str] = []

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(update_cmd.shutil, "which", lambda n: f"/opt/homebrew/bin/{n}")
    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)
    monkeypatch.setattr(
        dragon_cmd,
        "update_dragon_if_needed",
        lambda *, dry_run=False: (called.append(f"dragon:{dry_run}"), (True, "upgraded"))[1],
    )
    monkeypatch.setattr(
        yoda_cmd,
        "update_yoda_if_needed",
        lambda *, dry_run=False: (called.append(f"yoda:{dry_run}"), (True, "current"))[1],
    )

    res = runner.invoke(app, ["update", "--skip-self-test"])
    assert res.exit_code == 0, res.output
    assert called == ["dragon:False", "yoda:False"]

    # --cli-only skips Dragon and Yoda
    called.clear()
    res_only = runner.invoke(app, ["update", "--skip-self-test", "--cli-only"])
    assert res_only.exit_code == 0, res_only.output
    assert called == []
