"""Firewalla bridge pairing — fail-closed validation + config write.

Military-grade contract: onboarding may declare the bridge "paired" ONLY when
an AUTHENTICATED probe genuinely succeeds. A 401 (wrong token), a 000
(unreachable), or a malformed 200 must NOT be written as enabled — the headline
family feature (curfews) silently does nothing on an unpaired bridge, so a
false "paired" is worse than an honest "not paired".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import yaml

from sanctum_cli.commands import onboard, screen_time

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _mock_transport(monkeypatch: pytest.MonkeyPatch, *, status: int | None, body: object) -> None:
    """Patch httpx.get to simulate the bridge. status=None → connection error."""

    def fake_get(url, **kwargs):  # type: ignore[no-untyped-def]
        if status is None:
            raise httpx.ConnectError("simulated unreachable")
        req = httpx.Request("GET", url)
        import json as _j

        content = _j.dumps(body).encode() if body is not None else b"not json"
        return httpx.Response(status, request=req, content=content)

    monkeypatch.setattr("httpx.get", fake_get)


def test_pairing_paired_on_authenticated_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(monkeypatch, status=200, body=[{"mac": "AA:BB:CC:DD:EE:01", "name": "x"}])
    r = screen_time.validate_firewalla_pairing("http://127.0.0.1:1984", "good-token")
    assert r.state == "paired"
    assert r.ok is True


def test_pairing_fail_closed_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(monkeypatch, status=401, body={"error": "unauthorized"})
    r = screen_time.validate_firewalla_pairing("http://127.0.0.1:1984", "wrong-token")
    assert r.state == "auth_rejected"
    assert r.ok is False


def test_pairing_fail_closed_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(monkeypatch, status=None, body=None)
    r = screen_time.validate_firewalla_pairing("http://127.0.0.1:1984", "tok")
    assert r.state == "unreachable"
    assert r.ok is False


def test_pairing_fail_closed_on_malformed_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_transport(monkeypatch, status=200, body=None)  # 200 but not JSON
    r = screen_time.validate_firewalla_pairing("http://127.0.0.1:1984", "tok")
    assert r.ok is False
    assert r.state in ("bad_response", "unreachable")


def test_pairing_empty_token_is_unpaired(monkeypatch: pytest.MonkeyPatch) -> None:
    # No probe should even be attempted without a token.
    r = screen_time.validate_firewalla_pairing("http://127.0.0.1:1984", "")
    assert r.ok is False
    assert r.state == "no_token"


def test_set_firewalla_bridge_writes_only_on_paired(tmp_path: Path) -> None:
    inst = tmp_path / "instance.yaml"
    inst.write_text(
        "instance:\n  name: X\n  slug: x\nservices:\n  proxyd:\n    port: 4040\n", encoding="utf-8"
    )
    secrets = tmp_path / "fw-token"
    onboard.set_firewalla_bridge(
        path=inst,
        token="t0ken",
        device_ip="10.0.0.1",
        device_mac="AA:BB:CC:DD:EE:80",
        port=1984,
        token_file=secrets,
    )
    data = yaml.safe_load(inst.read_text(encoding="utf-8"))
    fb = data["services"]["firewalla_bridge"]
    assert fb["enabled"] is True
    assert fb["port"] == 1984
    assert fb["device_ip"] == "10.0.0.1"
    assert fb["device_mac"] == "AA:BB:CC:DD:EE:80"
    assert data["services"]["proxyd"]["port"] == 4040  # sibling untouched
    # token goes to the secrets file (mode 600), never into instance.yaml
    assert "t0ken" not in inst.read_text(encoding="utf-8")
    assert secrets.read_text(encoding="utf-8").strip() == "t0ken"
    assert (secrets.stat().st_mode & 0o777) == 0o600
