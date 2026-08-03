"""CLI tests for the ``sanctum tailnet`` group.

Every impure boundary in ``commands.tailnet`` is a module-level seam; these tests
patch those seams (never a live tailscale/API/keychain) and assert on the command
behaviour. The load-bearing safety assertion is that ``apply`` WITHOUT ``--apply``
fires the non-mutating validate but NEVER the mutating push.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.net.tailnet import (
    AclDrift,
    CredState,
    PeerReach,
    SpineState,
    TrifectaState,
)

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_MOD = "sanctum_cli.commands.tailnet"


class _Api:
    """Recording stub for ``_api_request``: logs (method, path), returns canned
    responses (default 200/``{}``)."""

    def __init__(self, responses: dict[tuple[str, str], tuple[int, str]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.responses = responses or {}

    def __call__(
        self,
        method: str,
        path: str,
        token: str,
        *,
        body: str | None = None,
        content_type: str | None = None,
        accept: str | None = None,
    ) -> tuple[int, str]:
        self.calls.append((method, path))
        return self.responses.get((method, path), (200, "{}"))


# ─── doctor ───────────────────────────────────────────────────────────


def test_doctor_renders_degraded_pane_with_acl_gap() -> None:
    with (
        patch(f"{_MOD}._probe_spine", return_value=SpineState(True, "tail1a2b.ts.net")),
        patch(f"{_MOD}._probe_peer", return_value=PeerReach("berts-mbp", True, False)),
        patch(f"{_MOD}._probe_cred", return_value=CredState(401, "keychain api-key")),
        patch(f"{_MOD}._probe_drift", return_value=AclDrift(None, "no cred")),
        patch(f"{_MOD}._probe_trifecta", return_value=TrifectaState(False, None, False)),
    ):
        result = runner.invoke(app, ["tailnet", "doctor"])
    assert result.exit_code == 0, result.stdout
    assert "DEGRADED" in result.stdout
    assert "ACL gap" in result.stdout


def test_doctor_json_all_green() -> None:
    with (
        patch(f"{_MOD}._probe_spine", return_value=SpineState(True, "")),
        patch(f"{_MOD}._probe_peer", return_value=PeerReach("berts-mbp", True, True)),
        patch(f"{_MOD}._probe_cred", return_value=CredState(200, "keychain oauth")),
        patch(f"{_MOD}._probe_drift", return_value=AclDrift(True, "matches")),
        patch(f"{_MOD}._probe_trifecta", return_value=TrifectaState(True, None, True)),
    ):
        result = runner.invoke(app, ["tailnet", "doctor", "--json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["overall"] == "GREEN"
    assert len(data["rows"]) == 5


def test_doctor_never_crashes_when_every_probe_fails() -> None:
    """A raising probe degrades its row to UNKNOWN; the pane still renders (exit 0)."""
    boom = MagicMock(side_effect=RuntimeError("probe blew up"))
    with (
        patch(f"{_MOD}._probe_spine", boom),
        patch(f"{_MOD}._probe_peer", boom),
        patch(f"{_MOD}._probe_cred", boom),
        patch(f"{_MOD}._probe_drift", boom),
        patch(f"{_MOD}._probe_trifecta", boom),
    ):
        result = runner.invoke(app, ["tailnet", "doctor"])
    assert result.exit_code == 0, result.stdout
    assert "UNKNOWN" in result.stdout


# ─── apply ────────────────────────────────────────────────────────────


def test_apply_dry_run_fires_no_push(tmp_path: Path) -> None:
    acl = tmp_path / "acl.hujson"
    acl.write_text('{"acls":[]}', encoding="utf-8")
    api = _Api({("POST", "tailnet/-/acl/validate"): (200, "{}")})
    with (
        patch(f"{_MOD}._resolve_token", return_value=("tok", "test")),
        patch(f"{_MOD}._api_request", api),
    ):
        result = runner.invoke(app, ["tailnet", "apply", "--acl", str(acl)])
    assert result.exit_code == 0, result.stdout
    assert ("POST", "tailnet/-/acl/validate") in api.calls
    assert ("POST", "tailnet/-/acl") not in api.calls  # the mutating push NEVER fired
    assert "dry-run" in result.stdout


def test_apply_pushes_and_verifies(tmp_path: Path) -> None:
    acl = tmp_path / "acl.hujson"
    body = '{"acls":[]}'
    acl.write_text(body, encoding="utf-8")
    api = _Api(
        {
            ("POST", "tailnet/-/acl/validate"): (200, "{}"),
            ("GET", "tailnet/-/acl"): (200, body),  # backup + post-verify re-read
            ("POST", "tailnet/-/acl"): (200, "{}"),
        }
    )
    with (
        patch(f"{_MOD}._resolve_token", return_value=("tok", "test")),
        patch(f"{_MOD}._api_request", api),
    ):
        result = runner.invoke(app, ["tailnet", "apply", "--apply", "--acl", str(acl)])
    assert result.exit_code == 0, result.stdout
    assert ("POST", "tailnet/-/acl") in api.calls  # the push fired
    assert "APPLIED" in result.stdout
    assert "verified" in result.stdout


def test_apply_without_credential_fails(tmp_path: Path) -> None:
    acl = tmp_path / "acl.hujson"
    acl.write_text('{"acls":[]}', encoding="utf-8")
    with patch(f"{_MOD}._resolve_token", return_value=(None, "none")):
        result = runner.invoke(app, ["tailnet", "apply", "--acl", str(acl)])
    assert result.exit_code != 0


def test_apply_fails_validation(tmp_path: Path) -> None:
    acl = tmp_path / "acl.hujson"
    acl.write_text('{"acls":[]}', encoding="utf-8")
    api = _Api({("POST", "tailnet/-/acl/validate"): (400, "bad policy")})
    with (
        patch(f"{_MOD}._resolve_token", return_value=("tok", "test")),
        patch(f"{_MOD}._api_request", api),
    ):
        result = runner.invoke(app, ["tailnet", "apply", "--apply", "--acl", str(acl)])
    assert result.exit_code != 0
    assert ("POST", "tailnet/-/acl") not in api.calls  # never pushed after a failed validate


# ─── creds ────────────────────────────────────────────────────────────


def test_creds_stores_when_both_scopes_present() -> None:
    api = _Api()  # default 200 for both /acl and /devices → both scopes present
    store = MagicMock()
    with (
        patch(f"{_MOD}._open_url"),
        patch(f"{_MOD}._mint_oauth_token", return_value="tok"),
        patch(f"{_MOD}._api_request", api),
        patch(f"{_MOD}._store_creds", store),
    ):
        result = runner.invoke(app, ["tailnet", "creds"], input="my-client-id\nmy-secret\n")
    assert result.exit_code == 0, result.stdout
    store.assert_called_once_with("my-client-id", "my-secret")


def test_creds_refuses_and_stores_nothing_when_scope_missing() -> None:
    api = _Api({("GET", "tailnet/-/devices"): (403, "")})  # devices scope missing
    store = MagicMock()
    with (
        patch(f"{_MOD}._open_url"),
        patch(f"{_MOD}._mint_oauth_token", return_value="tok"),
        patch(f"{_MOD}._api_request", api),
        patch(f"{_MOD}._store_creds", store),
    ):
        result = runner.invoke(app, ["tailnet", "creds"], input="cid\ncsec\n")
    assert result.exit_code != 0
    store.assert_not_called()  # keychain untouched when a scope is missing


def test_creds_fails_when_token_mint_fails() -> None:
    store = MagicMock()
    with (
        patch(f"{_MOD}._open_url"),
        patch(f"{_MOD}._mint_oauth_token", return_value=None),
        patch(f"{_MOD}._store_creds", store),
    ):
        result = runner.invoke(app, ["tailnet", "creds"], input="cid\ncsec\n")
    assert result.exit_code != 0
    store.assert_not_called()
