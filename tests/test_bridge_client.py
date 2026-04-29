"""BridgeClient — HMAC signing + HTTP layer."""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import patch

import httpx
import pytest
import respx

from sanctum_cli.bridge_client import (
    BridgeClient,
    BridgeCreds,
    base_url_from_env,
    encode_file,
)
from sanctum_cli.errors import NetworkError, ProviderError, UserError

CREDS = BridgeCreds(
    cf_access_id="cfid.access",
    cf_access_secret="cfsecret-x",
    hmac_secret="hmac-secret-deadbeef",
)


def _client_with(http: httpx.Client) -> BridgeClient:
    return BridgeClient(CREDS, base_url="https://bridge.test", http=http)


# ---------------------------------------------------------------- signing


def test_sign_includes_all_required_headers():
    c = BridgeClient(CREDS, base_url="https://bridge.test")
    h = c._sign(method="GET", path="/_manifest", body=b"")
    for name in (
        "User-Agent",
        "CF-Access-Client-Id",
        "CF-Access-Client-Secret",
        "Authorization",
        "X-Sanctum-Module",
        "X-Sanctum-Timestamp",
        "X-Sanctum-Nonce",
        "X-Sanctum-Signature",
    ):
        assert name in h, f"missing header {name!r}"
    assert h["Authorization"] == "SanctumHMAC v1"
    assert h["X-Sanctum-Module"] == "sharepoint"


def test_sign_signature_matches_canonical_string():
    c = BridgeClient(CREDS, base_url="https://bridge.test")
    body = b'{"hello":"world"}'
    h = c._sign(method="POST", path="/sharepoint/upload", body=body)
    expected = hmac.new(
        CREDS.hmac_secret.encode(),
        f"{h['X-Sanctum-Timestamp']}\n{h['X-Sanctum-Nonce']}\nPOST\n"
        f"/sharepoint/upload\n{hashlib.sha256(body).hexdigest()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert h["X-Sanctum-Signature"] == expected


def test_signed_nonces_differ_across_calls():
    c = BridgeClient(CREDS, base_url="https://bridge.test")
    a = c._sign(method="GET", path="/_health", body=b"")
    b = c._sign(method="GET", path="/_health", body=b"")
    assert a["X-Sanctum-Nonce"] != b["X-Sanctum-Nonce"]


# ----------------------------------------------------------------- health


@respx.mock
def test_health_returns_payload():
    route = respx.get("https://bridge.test/_health").mock(
        return_value=httpx.Response(200, json={"ok": True, "version": "0.1.0"})
    )
    with httpx.Client() as http:
        c = _client_with(http)
        out = c.health()
    assert route.called
    assert out["ok"] is True


# --------------------------------------------------------------- manifest


@respx.mock
def test_manifest_returns_modules_list():
    respx.get("https://bridge.test/_manifest").mock(
        return_value=httpx.Response(200, json={"modules": [{"name": "sharepoint"}]})
    )
    with httpx.Client() as http:
        c = _client_with(http)
        m = c.manifest()
    assert m["modules"][0]["name"] == "sharepoint"


# ----------------------------------------------------------------- upload


@respx.mock
def test_upload_sends_canonical_payload_and_returns_response():
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "drive_item_id": "item-x",
                "web_url": "https://sp/Y",
                "version": 1,
                "etag": "1",
                "metadata_applied": True,
            },
        )

    respx.post("https://bridge.test/sharepoint/upload").mock(side_effect=handle)
    with httpx.Client() as http:
        c = _client_with(http)
        res = c.upload(
            folder_path="Deals/X",
            filename="memo.txt",
            content_b64="aGVsbG8=",
            metadata={"doc_type": "investment-memo"},
            create_folder_if_missing=True,
            if_exists="version",
        )
    assert res["drive_item_id"] == "item-x"
    body = captured["body"]
    assert b'"folder_path":"Deals/X"' in body
    assert b'"filename":"memo.txt"' in body
    assert b'"if_exists":"version"' in body
    assert captured["headers"]["x-sanctum-module"] == "sharepoint"


# ------------------------------------------------------------ error mapping


@respx.mock
def test_403_destination_not_allowed_raises_provider_error_with_fix():
    respx.post("https://bridge.test/sharepoint/upload").mock(
        return_value=httpx.Response(
            403, json={"error": "destination_not_allowed", "path": "Random/X"}
        )
    )
    with httpx.Client() as http:
        c = _client_with(http)
        with pytest.raises(ProviderError) as exc:
            c.upload(
                folder_path="Random/X",
                filename="a",
                content_b64="",
                metadata={},
                create_folder_if_missing=True,
                if_exists="version",
            )
    assert "403" in exc.value.message
    assert "allowlist" in (exc.value.fix or "")


@respx.mock
def test_429_rate_limited_returns_provider_error_with_backoff_fix():
    respx.get("https://bridge.test/_manifest").mock(
        return_value=httpx.Response(429, json={"error": "rate_limited"})
    )
    with httpx.Client() as http:
        c = _client_with(http)
        with pytest.raises(ProviderError) as exc:
            c.manifest()
    assert "back off" in (exc.value.fix or "")


@respx.mock
def test_413_body_too_large_surfaces_byte_limit():
    respx.post("https://bridge.test/sharepoint/upload").mock(
        return_value=httpx.Response(
            413, json={"error": "body_too_large", "limit_bytes": 262144000}
        )
    )
    with httpx.Client() as http:
        c = _client_with(http)
        with pytest.raises(ProviderError) as exc:
            c.upload(
                folder_path="Deals/X",
                filename="big",
                content_b64="",
                metadata={},
                create_folder_if_missing=True,
                if_exists="version",
            )
    assert "262144000" in (exc.value.fix or "")


@respx.mock
def test_transport_failure_raises_network_error():
    respx.get("https://bridge.test/_manifest").mock(
        side_effect=httpx.ConnectError("nope")
    )
    with httpx.Client() as http:
        c = _client_with(http)
        with pytest.raises(NetworkError):
            c.manifest()


# ----------------------------------------------------------------- env / files


def test_base_url_from_env_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("SANCTUM_BRIDGE_URL", "https://bridge.staging.test/")
    assert base_url_from_env() == "https://bridge.staging.test"
    monkeypatch.delenv("SANCTUM_BRIDGE_URL")
    assert base_url_from_env() == "https://bridge.nepveu.name"


def test_encode_file_round_trips(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"hello world")
    name, b64 = encode_file(p)
    assert name == "x.txt"
    import base64

    assert base64.b64decode(b64) == b"hello world"


def test_encode_file_rejects_non_files(tmp_path):
    with pytest.raises(UserError):
        encode_file(tmp_path)


def test_from_keychain_user_error_when_entry_missing():
    from sanctum_cli import keychain as kc

    with patch.object(
        kc, "read", side_effect=kc.KeychainEntryMissingError("missing")
    ):
        with pytest.raises(UserError) as exc:
            BridgeCreds.from_keychain()
    assert "Keychain" in exc.value.message
