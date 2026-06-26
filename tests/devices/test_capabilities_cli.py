"""``sanctum net <kind> capabilities`` — the per-setting transport + ceiling list.

The capability-listing surface renders, for the resolved provider, the honest
multi-transport plan:

* the **API (live)** settings — each advertised capability with the concrete real
  op that backs it (driven now); and
* the **GUI-only ceiling** — the surfaces the API cannot reach, each marked with
  its Phase-2 fallback transport (agent-browser for the web-UI hubs, android for
  the app-only Firewalla) and the ``Phase 2: live recipe`` marker.

Driven end-to-end through Typer's ``CliRunner`` against the REAL provider classes
(so the rendered plan is derived from the genuine ``capability_map``), with the
network/vendor seams stubbed so no socket, no SOAP, no live box is touched. The
command is read-only — it never mutates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from sanctum_cli.cli import app
from sanctum_cli.devices import firewalla as fw_mod
from sanctum_cli.devices.base import Creds, NetContext
from sanctum_cli.devices.firewalla import FirewallaProvider
from sanctum_cli.devices.orbi import OrbiProvider
from sanctum_cli.devices.sagemcom import SagemcomHubProvider

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


def _ctx() -> NetContext:
    return NetContext(gateway_ip="192.168.2.1", runner=None)


def _creds() -> Creds:
    return Creds(host="192.168.2.1", username="admin", secret=None, key_path=None)


def _stub_lifecycle(monkeypatch: pytest.MonkeyPatch, provider_cls: type) -> None:
    """No-op connect/disconnect so the read-only listing never opens a transport."""
    monkeypatch.setattr(provider_cls, "connect", lambda self, creds: None)
    monkeypatch.setattr(provider_cls, "disconnect", lambda self: None)


# ── hub (Sagemcom → agent-browser ceiling) ───────────────────────────────────


def test_net_hub_capabilities_lists_api_and_browser_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = SagemcomHubProvider()
    _stub_lifecycle(monkeypatch, SagemcomHubProvider)
    monkeypatch.setattr(
        "sanctum_cli.commands.net.registry.resolve", lambda *a, **k: p
    )
    monkeypatch.setattr("sanctum_cli.commands.net._hub_netcontext", _ctx)
    monkeypatch.setattr("sanctum_cli.commands.net._hub_creds", lambda net: _creds())

    result = runner.invoke(app, ["net", "hub", "capabilities"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    # An API-backed capability with its concrete real op.
    assert "bridge_mode" in out
    assert "setbridgemode" in out
    # The GUI fallback transport for the carrier-locked ceiling.
    assert "agent-browser" in out
    assert "access_restriction" in out or "carrier" in out
    assert "phase 2" in out


# ── firewalla (app-only → android ceiling) ───────────────────────────────────


def test_net_firewalla_capabilities_lists_android_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # /info unreachable → enforcement UNKNOWN → enforcement caps kept (routes exist).
    monkeypatch.setattr(fw_mod, "_fetch_bridge_json", lambda *a, **k: None)
    p = FirewallaProvider()
    _stub_lifecycle(monkeypatch, FirewallaProvider)
    monkeypatch.setattr(
        "sanctum_cli.commands.net.registry.resolve", lambda *a, **k: p
    )
    monkeypatch.setattr("sanctum_cli.commands.net._firewalla_netcontext", _ctx)
    monkeypatch.setattr(
        "sanctum_cli.commands.net._firewalla_creds", lambda net: _creds()
    )

    result = runner.invoke(app, ["net", "firewalla", "capabilities"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    # A real bridge-route API capability.
    assert "/dns" in out or "local_dns" in out
    # The app-only ceiling routes to android, named with the WAN/NAT/DMZ walls.
    assert "android" in out
    assert "wan" in out and "nat" in out
    assert "phase 2" in out


# ── orbi (web UI / app → agent-browser ceiling; AP_MODE/CHANNELS defects) ─────


def test_net_orbi_capabilities_shows_defects_as_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = OrbiProvider()
    _stub_lifecycle(monkeypatch, OrbiProvider)
    monkeypatch.setattr(
        "sanctum_cli.commands.net.registry.resolve", lambda *a, **k: p
    )
    monkeypatch.setattr("sanctum_cli.commands.net._orbi_netcontext", _ctx)
    monkeypatch.setattr("sanctum_cli.commands.net._orbi_creds", lambda net: _creds())

    result = runner.invoke(app, ["net", "orbi", "capabilities"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    # A real pynetgear write capability is API-backed.
    assert "guest_wifi" in out
    # The honesty defects appear only in the GUI-only ceiling (agent-browser).
    assert "agent-browser" in out
    assert "channel" in out  # CHANNELS surface named in the ceiling
    assert "ap" in out  # AP_MODE surface named in the ceiling
    assert "phase 2" in out


def test_net_orbi_capabilities_disconnects_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capabilities command must release the provider via disconnect()."""
    p = OrbiProvider()
    seen: dict[str, bool] = {}
    monkeypatch.setattr(OrbiProvider, "connect", lambda self, creds: None)
    monkeypatch.setattr(
        OrbiProvider, "disconnect", lambda self: seen.__setitem__("dc", True)
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.net.registry.resolve", lambda *a, **k: p
    )
    monkeypatch.setattr("sanctum_cli.commands.net._orbi_netcontext", _ctx)
    monkeypatch.setattr("sanctum_cli.commands.net._orbi_creds", lambda net: _creds())

    result = runner.invoke(app, ["net", "orbi", "capabilities"])
    assert result.exit_code == 0, result.stdout
    assert seen.get("dc") is True
