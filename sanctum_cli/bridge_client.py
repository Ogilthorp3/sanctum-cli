"""HTTP client for the Sanctum Bridge.

The bridge sits behind two auth layers:

  1. **Cloudflare Access service token** — every request carries
     ``CF-Access-Client-Id`` and ``CF-Access-Client-Secret`` headers. CF
     Access verifies them at the edge and forwards a signed JWT to the
     origin (the bridge re-verifies that JWT against CF's JWKS).
  2. **Sanctum HMAC v1** — the origin requires
     ``Authorization: SanctumHMAC v1`` plus a timestamp, nonce, and
     signature. The signature is HMAC-SHA256 over a canonical string
     covering timestamp + nonce + method + path + sha256(body), keyed by
     the per-module secret.

This client implements both. Construct it with credentials read from the
Keychain via ``BridgeClient.from_keychain()``; the calling command then
uses high-level methods (``health``, ``manifest``, ``upload``,
``folder``) without thinking about signing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from sanctum_cli import config, keychain
from sanctum_cli.errors import NetworkError, ProviderError, UserError

DEFAULT_TIMEOUT_S = 30.0
USER_AGENT = "sanctum-cli/bridge"

# Keychain layout — mirrors what /opt/sanctum/bridge writes during setup.
_KC_CF_ID = ("client_id", "sanctum-bridge-cf-access-client-id")
_KC_CF_SECRET = ("client_secret", "sanctum-bridge-cf-access-client-secret")
_KC_HMAC = ("hmac_sharepoint", "sanctum-bridge-hmac-sharepoint")


@dataclass(frozen=True)
class BridgeCreds:
    cf_access_id: str
    cf_access_secret: str
    hmac_secret: str

    @classmethod
    def from_keychain(cls) -> BridgeCreds:
        try:
            return cls(
                cf_access_id=keychain.read(*_KC_CF_ID),
                cf_access_secret=keychain.read(*_KC_CF_SECRET),
                hmac_secret=keychain.read(*_KC_HMAC),
            )
        except keychain.KeychainEntryMissingError as exc:
            msg = f"missing bridge credential in Keychain: {exc}"
            raise UserError(
                msg,
                fix=(
                    "run the bridge bootstrap on the gateway host first; the "
                    "expected services are sanctum-bridge-cf-access-client-id, "
                    "sanctum-bridge-cf-access-client-secret, "
                    "sanctum-bridge-hmac-sharepoint."
                ),
            ) from exc


class BridgeClient:
    def __init__(
        self,
        creds: BridgeCreds,
        *,
        base_url: str,
        module: str = "sharepoint",
        timeout: float = DEFAULT_TIMEOUT_S,
        http: httpx.Client | None = None,
    ) -> None:
        self._creds = creds
        self.base_url = base_url.rstrip("/")
        self.module = module
        self._timeout = timeout
        # Allow the test suite to inject an httpx.Client wired to a respx
        # transport. In production we own the client and close it on exit.
        self._http = http
        self._owns_http = http is None

    # ---------------------------------------------------------------- helpers
    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout, headers={"User-Agent": USER_AGENT})
        return self._http

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> BridgeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _sign(self, *, method: str, path: str, body: bytes) -> dict[str, str]:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = str(uuid.uuid4())
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = f"{ts}\n{nonce}\n{method}\n{path}\n{body_hash}"
        sig = hmac.new(
            self._creds.hmac_secret.encode(),
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "User-Agent": USER_AGENT,
            "CF-Access-Client-Id": self._creds.cf_access_id,
            "CF-Access-Client-Secret": self._creds.cf_access_secret,
            "Authorization": "SanctumHMAC v1",
            "X-Sanctum-Module": self.module,
            "X-Sanctum-Timestamp": ts,
            "X-Sanctum-Nonce": nonce,
            "X-Sanctum-Signature": sig,
        }

    def _request(self, method: str, path: str, *, body: bytes = b"") -> Any:
        headers = self._sign(method=method, path=path, body=body)
        if body:
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}{path}"
        try:
            resp = self._client().request(method, url, headers=headers, content=body)
        except httpx.HTTPError as exc:
            msg = f"bridge unreachable: {exc}"
            raise NetworkError(msg, fix="check the tunnel and the bridge daemon") from exc
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except json.JSONDecodeError:
                payload = {"raw": resp.text[:200]}
            code = payload.get("code") or payload.get("error") or "unknown"
            raise ProviderError(
                f"bridge {method} {path} → HTTP {resp.status_code} ({code})",
                fix=_fix_for(resp.status_code, payload),
            )
        if not resp.content:
            return None
        return resp.json()

    # ----------------------------------------------------------------- public
    def health(self) -> dict[str, Any]:
        return cast("dict[str, Any]", self._request("GET", "/_health"))

    def manifest(self) -> dict[str, Any]:
        return cast("dict[str, Any]", self._request("GET", "/_manifest"))

    def diagnostic(self) -> dict[str, Any]:
        return cast("dict[str, Any]", self._request("GET", "/_diagnostic"))

    def folder(self, path: str) -> dict[str, Any]:
        body = json.dumps({"path": path}, separators=(",", ":")).encode()
        return cast("dict[str, Any]", self._request("POST", "/sharepoint/folder", body=body))

    def children(self, path: str) -> dict[str, Any]:
        """List the children of a SharePoint folder (read)."""
        body = json.dumps({"path": path}, separators=(",", ":")).encode()
        return cast("dict[str, Any]", self._request("POST", "/sharepoint/children", body=body))

    def download(self, path: str, *, extract_text: bool = False) -> dict[str, Any]:
        """Download a SharePoint file (read). With ``extract_text`` the bridge
        also returns best-effort extracted plain text for .docx/.pdf/.xlsx."""
        payload = {"path": path, "extract_text": extract_text}
        body = json.dumps(payload, separators=(",", ":")).encode()
        return cast("dict[str, Any]", self._request("POST", "/sharepoint/download", body=body))

    def rename(self, path: str, new_name: str) -> dict[str, Any]:
        """Rename a SharePoint file or folder in place (write).

        ``new_name`` is a bare name — not a path. The bridge gates this on the
        write allowlist (same as ``upload``) and rejects a ``new_name`` that
        carries a path separator."""
        payload = {"path": path, "new_name": new_name}
        body = json.dumps(payload, separators=(",", ":")).encode()
        return cast("dict[str, Any]", self._request("POST", "/sharepoint/rename", body=body))

    def search(
        self, query: str, *, folder_path: str | None = None, top: int = 25
    ) -> dict[str, Any]:
        """Search SharePoint for items matching ``query`` (read).

        ``folder_path`` scopes the search to a sub-folder (``None`` = whole
        drive). The bridge gates a scoped search on the read scope. Returns
        ``{"results": [...], "truncated": bool}``."""
        payload: dict[str, Any] = {"query": query, "folder_path": folder_path, "top": top}
        body = json.dumps(payload, separators=(",", ":")).encode()
        return cast("dict[str, Any]", self._request("POST", "/sharepoint/search", body=body))

    def delete(self, path: str) -> dict[str, Any]:
        """Delete a SharePoint file or folder (write).

        The bridge gates this on the WRITE allowlist (same as ``upload`` /
        ``rename``). The item goes to the site Recycle Bin — it is recoverable,
        not hard-purged. Returns ``{"deleted": true, "path": str}``."""
        payload = {"path": path}
        body = json.dumps(payload, separators=(",", ":")).encode()
        return cast("dict[str, Any]", self._request("POST", "/sharepoint/delete", body=body))

    def move(self, path: str, dest_folder: str, *, new_name: str | None = None) -> dict[str, Any]:
        """Move a SharePoint item into ``dest_folder`` (write).

        The bridge gates BOTH the source ``path`` AND the ``dest_folder`` on the
        WRITE allowlist — you can only move within allowed roots. ``new_name``,
        when given, must be a bare name (no path separator) and renames the item
        at its destination in the same call."""
        payload: dict[str, Any] = {"path": path, "dest_folder": dest_folder, "new_name": new_name}
        body = json.dumps(payload, separators=(",", ":")).encode()
        return cast("dict[str, Any]", self._request("POST", "/sharepoint/move", body=body))

    def upload(
        self,
        *,
        folder_path: str,
        filename: str,
        content_b64: str,
        metadata: dict[str, str] | None = None,
        create_folder_if_missing: bool = True,
        if_exists: str = "version",
    ) -> dict[str, Any]:
        payload = {
            "folder_path": folder_path,
            "filename": filename,
            "content_b64": content_b64,
            "metadata": metadata or {},
            "create_folder_if_missing": create_folder_if_missing,
            "if_exists": if_exists,
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        return cast("dict[str, Any]", self._request("POST", "/sharepoint/upload", body=body))


def encode_file(path: Path | str) -> tuple[str, str]:
    """Return ``(filename, base64_body)`` for the file at ``path``."""
    p = Path(path)
    if not p.is_file():
        msg = f"not a file: {p}"
        raise UserError(msg, fix="pass a path to a regular file")
    return p.name, base64.b64encode(p.read_bytes()).decode("ascii")


def base_url_from_env() -> str:
    """Bridge URL — no baked-in personal host.

    Resolution order (discovery-first):
      1. ``$SANCTUM_BRIDGE_URL`` (per-invocation override);
      2. instance.yaml ``secrets.cloudflare_bridge_domain`` (operator config);
      3. raise :class:`UserError` — there is no personal fallback to ship.
    """
    env = os.environ.get("SANCTUM_BRIDGE_URL")
    if env:
        return env.rstrip("/")
    domain = config.instance_value("secrets.cloudflare_bridge_domain", None)
    if domain:
        return f"https://{domain}".rstrip("/")
    msg = "bridge URL is not configured"
    raise UserError(
        msg,
        fix=(
            "set secrets.cloudflare_bridge_domain in ~/.sanctum/instance.yaml "
            "or export SANCTUM_BRIDGE_URL"
        ),
    )


def _configured_bridge_url() -> str:
    """The configured bridge URL for hint text, or a generic phrase if unset.

    Never raises — hints are best-effort and must not mask the real error."""
    try:
        return base_url_from_env()
    except UserError:
        return "your bridge's public hostname"


def _fix_for(status: int, body: dict[str, Any]) -> str:
    if status == 401:
        code = body.get("code", "")
        if code == "jwt_missing" or body.get("error") == "cf_access_failed":
            return (
                "request did not carry a CF Access JWT; bypassing CF? Hit the "
                f"public hostname ({_configured_bridge_url()}) instead of "
                "127.0.0.1."
            )
        if code in {"signature_mismatch", "missing_scheme", "missing_headers"}:
            return "rotate sanctum-bridge-hmac-sharepoint or check clock skew"
        if code == "nonce_replay":
            return "this nonce was used recently; client must generate a fresh one"
        if code == "clock_skew":
            return "host clock is more than 60s off UTC; sync NTP"
    if status == 400 and body.get("error") == "invalid_argument":
        return (
            "bad request input; for rename, new_name must be a bare file/folder "
            "name (no '/' or '\\') and not empty."
        )
    if status == 403 and body.get("error") == "destination_not_allowed":
        return (
            "folder root is not on the bridge allowlist; edit your bridge's "
            "sharepoint-structure.yaml allowlist and wait up to an hour "
            "for the bridge to refresh, or restart it."
        )
    if status == 413:
        limit = body.get("limit_bytes")
        return f"body exceeds the bridge cap ({limit} bytes); split the upload"
    if status == 429:
        return "per-client rate limit hit; back off ~1s and retry"
    return ""
