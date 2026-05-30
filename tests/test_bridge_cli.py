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


def test_bridge_children_lists_entries():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/children").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "children": [
                            {
                                "name": "Memos",
                                "drive_item_id": "F1",
                                "is_folder": True,
                                "is_file": False,
                                "size": 0,
                                "web_url": "https://sp/Memos",
                                "last_modified": "2026-05-01T00:00:00Z",
                                "child_count": 4,
                            },
                            {
                                "name": "term-sheet.pdf",
                                "drive_item_id": "D1",
                                "is_folder": False,
                                "is_file": True,
                                "size": 2048,
                                "web_url": "https://sp/ts.pdf",
                                "last_modified": "2026-05-02T00:00:00Z",
                                "child_count": None,
                            },
                        ],
                        "truncated": False,
                    },
                )
            )
            r = runner.invoke(app, ["bridge", "children", "Deals/Calder"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "Memos" in r.output
    assert "term-sheet.pdf" in r.output


def test_bridge_children_json():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/children").mock(
                return_value=httpx.Response(
                    200, json={"children": [], "truncated": True}
                )
            )
            r = runner.invoke(app, ["bridge", "children", "Deals", "--json"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert '"truncated"' in r.output


def test_bridge_children_warns_when_truncated():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/children").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "children": [
                            {
                                "name": "x.txt",
                                "drive_item_id": "D",
                                "is_folder": False,
                                "is_file": True,
                                "size": 1,
                                "web_url": "https://sp/x",
                                "last_modified": "",
                                "child_count": None,
                            }
                        ],
                        "truncated": True,
                    },
                )
            )
            r = runner.invoke(app, ["bridge", "children", "Big"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "truncated" in r.output.lower()


def test_bridge_download_writes_bytes_to_out(tmp_path):
    out = tmp_path / "got.txt"
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/download").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "content_b64": "aGVsbG8gd29ybGQ=",  # "hello world"
                        "size": 11,
                        "content_type": "text/plain",
                        "extracted": False,
                        "text": None,
                    },
                )
            )
            r = runner.invoke(
                app,
                ["bridge", "download", "Deals/Calder/memo.txt", "--out", str(out)],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert out.read_bytes() == b"hello world"


def test_bridge_download_extract_text_prints_text(tmp_path):
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "content_b64": "UEs=",
                "size": 2,
                "content_type": "application/pdf",
                "extracted": True,
                "text": "Calder term sheet body",
            },
        )

    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/download").mock(side_effect=handle)
            r = runner.invoke(
                app,
                ["bridge", "download", "Deals/ts.pdf", "--extract-text"],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "Calder term sheet body" in r.output
    assert b'"extract_text":true' in captured["body"]


def test_bridge_rename_happy_path():
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "drive_item_id": "01ITEM",
                "web_url": "https://sp/renamed",
                "name": "investment-memo.docx",
            },
        )

    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/rename").mock(side_effect=handle)
            r = runner.invoke(
                app,
                [
                    "bridge",
                    "rename",
                    "Deals/Calder/Memos/draft.docx",
                    "investment-memo.docx",
                ],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "renamed" in r.output
    assert "investment-memo.docx" in r.output
    assert "01ITEM" in r.output
    body = captured["body"]
    assert b'"path":"Deals/Calder/Memos/draft.docx"' in body
    assert b'"new_name":"investment-memo.docx"' in body


def test_bridge_rename_json():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/rename").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "drive_item_id": "01ITEM",
                        "web_url": "https://sp/renamed",
                        "name": "new.docx",
                    },
                )
            )
            r = runner.invoke(
                app,
                ["bridge", "rename", "Deals/old.docx", "new.docx", "--json"],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert '"drive_item_id"' in r.output
    assert '"name"' in r.output


def test_bridge_rename_blocked_path_surfaces_error():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/rename").mock(
                return_value=httpx.Response(
                    403,
                    json={"error": "destination_not_allowed", "path": "Random/X/memo.docx"},
                )
            )
            r = runner.invoke(
                app,
                ["bridge", "rename", "Random/X/memo.docx", "new.docx"],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code != 0
    assert "allowlist" in r.output


def test_bridge_rename_rejects_new_name_with_slash_client_side():
    """A new_name with a path separator is bad input — the CLI should reject it
    before any network call (no respx mock needed)."""
    patches = _patches()
    _enter_all(patches)
    try:
        r = runner.invoke(
            app,
            ["bridge", "rename", "Deals/memo.docx", "sub/new.docx"],
        )
    finally:
        _exit_all(patches)
    assert r.exit_code != 0
    assert "name" in r.output.lower()


# ------------------------------------------------------------------- search


def test_bridge_search_lists_hits():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/search").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "name": "Calder",
                                "drive_item_id": "F1",
                                "is_folder": True,
                                "is_file": False,
                                "size": 0,
                                "web_url": "https://sp/Calder",
                                "parent_path": "Deals",
                            },
                            {
                                "name": "term-sheet.pdf",
                                "drive_item_id": "D1",
                                "is_folder": False,
                                "is_file": True,
                                "size": 2048,
                                "web_url": "https://sp/ts.pdf",
                                "parent_path": "Deals/Calder",
                            },
                        ],
                        "truncated": False,
                    },
                )
            )
            r = runner.invoke(app, ["bridge", "search", "Calder"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "Calder" in r.output
    assert "term-sheet.pdf" in r.output
    # parent_path is surfaced — the keystone for locating a deal folder.
    assert "Deals" in r.output


def test_bridge_search_scoped_sends_folder_and_top():
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"results": [], "truncated": False})

    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/search").mock(side_effect=handle)
            r = runner.invoke(
                app,
                ["bridge", "search", "memo", "--folder", "Deals/Calder", "--top", "5"],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    body = captured["body"]
    assert b'"folder_path":"Deals/Calder"' in body
    assert b'"top":5' in body


def test_bridge_search_json():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/search").mock(
                return_value=httpx.Response(200, json={"results": [], "truncated": True})
            )
            r = runner.invoke(app, ["bridge", "search", "x", "--json"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert '"truncated"' in r.output


def test_bridge_search_warns_when_truncated():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/search").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "name": "x",
                                "drive_item_id": "D",
                                "is_folder": False,
                                "is_file": True,
                                "size": 1,
                                "web_url": "https://sp/x",
                                "parent_path": "Deals",
                            }
                        ],
                        "truncated": True,
                    },
                )
            )
            r = runner.invoke(app, ["bridge", "search", "x", "--top", "1"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "truncated" in r.output.lower()


# ------------------------------------------------------------------- delete


def test_bridge_delete_requires_yes_flag():
    """Without --yes the delete must be refused BEFORE any network call — the
    destructive-action guard."""
    patches = _patches()
    _enter_all(patches)
    try:
        # No respx mock: if the command hits the network, the test fails loudly.
        r = runner.invoke(app, ["bridge", "delete", "Deals/Calder/old.docx"])
    finally:
        _exit_all(patches)
    assert r.exit_code != 0
    assert "yes" in r.output.lower() or "confirm" in r.output.lower()


def test_bridge_delete_with_yes_happy_path():
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"deleted": True, "path": "Deals/Calder/old.docx"})

    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/delete").mock(side_effect=handle)
            r = runner.invoke(app, ["bridge", "delete", "Deals/Calder/old.docx", "--yes"])
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "deleted" in r.output.lower()
    assert "recycle bin" in r.output.lower()
    assert b'"path":"Deals/Calder/old.docx"' in captured["body"]


def test_bridge_delete_blocked_path_surfaces_error():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/delete").mock(
                return_value=httpx.Response(
                    403,
                    json={"error": "destination_not_allowed", "path": "Random/X/old.docx"},
                )
            )
            r = runner.invoke(app, ["bridge", "delete", "Random/X/old.docx", "--yes"])
    finally:
        _exit_all(patches)
    assert r.exit_code != 0
    assert "allowlist" in r.output


# ------------------------------------------------------------------- move


def test_bridge_move_happy_path():
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "drive_item_id": "src-id",
                "web_url": "https://sp/moved",
                "name": "memo.docx",
                "parent_path": "Deals/Archive",
            },
        )

    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/move").mock(side_effect=handle)
            r = runner.invoke(
                app,
                ["bridge", "move", "Deals/Calder/memo.docx", "Deals/Archive"],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "moved" in r.output
    assert "Deals/Archive" in r.output
    body = captured["body"]
    assert b'"path":"Deals/Calder/memo.docx"' in body
    assert b'"dest_folder":"Deals/Archive"' in body
    assert b'"new_name":null' in body


def test_bridge_move_with_name():
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "drive_item_id": "src-id",
                "web_url": "https://sp/moved",
                "name": "final.docx",
                "parent_path": "Deals/Archive",
            },
        )

    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/move").mock(side_effect=handle)
            r = runner.invoke(
                app,
                ["bridge", "move", "Deals/memo.docx", "Deals/Archive", "--name", "final.docx"],
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert "final.docx" in r.output
    assert b'"new_name":"final.docx"' in captured["body"]


def test_bridge_move_json():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/move").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "drive_item_id": "src-id",
                        "web_url": "https://sp/moved",
                        "name": "memo.docx",
                        "parent_path": "Deals/Archive",
                    },
                )
            )
            r = runner.invoke(
                app, ["bridge", "move", "Deals/memo.docx", "Deals/Archive", "--json"]
            )
    finally:
        _exit_all(patches)
    assert r.exit_code == 0, r.output
    assert '"parent_path"' in r.output


def test_bridge_move_rejects_name_with_slash_client_side():
    """A --name with a path separator is bad input — the CLI should reject it
    before any network call (no respx mock needed)."""
    patches = _patches()
    _enter_all(patches)
    try:
        r = runner.invoke(
            app,
            ["bridge", "move", "Deals/memo.docx", "Deals/Archive", "--name", "sub/new.docx"],
        )
    finally:
        _exit_all(patches)
    assert r.exit_code != 0
    assert "name" in r.output.lower()


def test_bridge_move_blocked_dest_surfaces_error():
    patches = _patches()
    _enter_all(patches)
    try:
        with respx.mock(assert_all_called=False) as rx:
            rx.post("https://bridge.test/sharepoint/move").mock(
                return_value=httpx.Response(
                    403,
                    json={"error": "destination_not_allowed", "path": "Random/X"},
                )
            )
            r = runner.invoke(
                app, ["bridge", "move", "Deals/memo.docx", "Random/X"]
            )
    finally:
        _exit_all(patches)
    assert r.exit_code != 0
    assert "allowlist" in r.output


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
