"""``sanctum bridge`` Typer commands — BridgeClient mocked at the network."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import respx
from typer.testing import CliRunner

from sanctum_cli.bridge_client import BridgeCreds
from sanctum_cli.cli import app

runner = CliRunner()


CREDS = BridgeCreds(
    cf_access_id="cfid.access",
    cf_access_secret="secret-zzzzzzzzzzzz-tail",
    hmac_secret="hmac-aaaaaaaaaaaaaa-tail",
)


def _patches():
    """Common patches for the Keychain + base URL during CLI invocations."""
    return [
        patch(
            "sanctum_cli.bridge_client.BridgeCreds.from_keychain",
            return_value=CREDS,
        ),
        patch(
            "sanctum_cli.bridge_client.base_url_from_env",
            return_value="https://bridge.test",
        ),
        patch(
            "sanctum_cli.commands.bridge.base_url_from_env",
            return_value="https://bridge.test",
        ),
    ]


def _enter_all(patches):
    return [p.__enter__() for p in patches]


def _exit_all(patches):
    for p in patches:
        p.__exit__(None, None, None)


# ------------------------------------------------------------------- health


def test_bridge_health_prints_modules():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.get("https://bridge.test/_health").mock(
                return_value=httpx.Response(
                    200, json={"ok": True, "version": "0.1.0", "modules": ["sharepoint"]}
                )
            )
            r = runner.invoke(app, ["bridge", "health"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "sharepoint" in r.output
    assert "v0.1.0" in r.output


def test_bridge_health_json():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.get("https://bridge.test/_health").mock(
                return_value=httpx.Response(
                    200, json={"ok": True, "version": "0.1.0", "modules": ["sharepoint"]}
                )
            )
            r = runner.invoke(app, ["bridge", "health", "--json"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0
    assert '"ok"' in r.output


# ------------------------------------------------------------------ whoami


def test_bridge_whoami_redacts_secrets():
    patches = _patches()
    _enter_all(patches)
    try:
        r = runner.invoke(app, ["bridge", "whoami"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0
    assert "cfid.access" in r.output
    # The full secret must never be in the output; redacted form only.
    assert "secret-zzzzzzzzzzzz-tail" not in r.output
    assert "hmac-aaaaaaaaaaaaaa-tail" not in r.output
    assert "redacted" in r.output


# ----------------------------------------------------------------- manifest


def test_bridge_manifest_lists_actions():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.get("https://bridge.test/_manifest").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "modules": [
                            {
                                "name": "sharepoint",
                                "version": "0.1.0",
                                "actions": [
                                    {
                                        "name": "upload",
                                        "method": "POST",
                                        "path": "/upload",
                                        "summary": "Upload a file",
                                        "request_schema": None,
                                        "response_schema": None,
                                    }
                                ],
                            }
                        ]
                    },
                )
            )
            r = runner.invoke(app, ["bridge", "manifest"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0
    assert "sharepoint.upload" in r.output
    assert "POST /sharepoint/upload" in r.output


# ------------------------------------------------------------------- upload


def test_bridge_upload_happy_path(tmp_path):
    f = tmp_path / "memo.txt"
    f.write_text("hello bridge")
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/upload").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "drive_item_id": "01ITEM",
                        "web_url": "https://sp/X",
                        "version": 3,
                        "etag": "abc",
                        "metadata_applied": True,
                    },
                )
            )
            r = runner.invoke(
                app,
                [
                    "bridge",
                    "upload",
                    str(f),
                    "Deals/Calder/Memos",
                    "--doc-type",
                    "investment-memo",
                    "--if-exists",
                    "version",
                ],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "uploaded" in r.output
    assert "01ITEM" in r.output
    assert "version" in r.output


def test_bridge_upload_warns_when_metadata_not_applied(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/upload").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "drive_item_id": "01ITEM",
                        "web_url": "https://sp/X",
                        "version": None,
                        "etag": "abc",
                        "metadata_applied": False,
                    },
                )
            )
            r = runner.invoke(
                app,
                [
                    "bridge",
                    "upload",
                    str(f),
                    "Deals/X",
                    "--doc-type",
                    "investment-memo",
                ],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0
    # CliRunner merges stdout + stderr by default; warning should land in output.
    assert "metadata patch" in r.output or "metadata patch" in (r.stderr or "")


def test_bridge_upload_bad_if_exists_value(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"x")
    patches = _patches()
    _enter_all(patches)
    try:
        r = runner.invoke(
            app,
            ["bridge", "upload", str(f), "Deals/X", "--if-exists", "wrong"],
        )
    finally:
        _exit_all(patches)
    assert r.exit_code != 0
    assert "version|overwrite|rename|fail" in r.output


def test_bridge_upload_metadata_kv_parsing(tmp_path):
    """`-m k=v -m a=b` round-trips into the upload payload."""
    f = tmp_path / "x.txt"
    f.write_bytes(b"x")
    captured = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "drive_item_id": "i",
                "web_url": "https://sp/Y",
                "version": 1,
                "etag": "e",
                "metadata_applied": True,
            },
        )

    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/upload").mock(side_effect=handle)
            r = runner.invoke(
                app,
                [
                    "bridge",
                    "upload",
                    str(f),
                    "Deals/X",
                    "-m",
                    "company=Calder",
                    "-m",
                    "stage=IC",
                ],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    body = captured["body"]
    assert b'"company":"Calder"' in body
    assert b'"stage":"IC"' in body
