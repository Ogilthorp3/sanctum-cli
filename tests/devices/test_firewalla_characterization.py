"""Characterization tests — pin the CURRENT Firewalla-bridge read behavior.

Safety net for the Phase-2 Firewalla refactor. These tests assert the
*existing* contract of the bridge-reading internals in
``sanctum_cli.commands.screen_time`` so a later refactor (moving this surface
behind the DeviceProvider abstraction) cannot silently change behavior. They
are GREEN against the unchanged code today and must stay green afterward.

Scope deliberately covers the seams the existing
``test_screen_time_compat`` / ``test_firewalla_pairing`` suites do NOT pin:

* ``_fetch_bridge_json`` HTTP transport — URL construction, the bearer header,
  ``$FIREWALLA_BRIDGE_URL`` override, the token-file fallback, and the
  fail-soft (``None``) behavior on non-200 / non-JSON / non-dict / transport
  error.
* ``_managed_macs`` — which MACs the engine harvests from a devices.yaml
  (family personal devices + shared_devices + screens), upper-cased + sorted.
* ``compat_command``'s spoof-mode ``/host/<mac>`` monitoring loop — that it
  builds the ``monitored`` map from per-host fetches and feeds it to
  ``assess_compat``.

SAFETY: every test mocks the bridge HTTP (``httpx.get``); none touches the
live Firewalla, and the real on-disk token file is never read — the URL/token
come from the environment or a monkeypatched module constant pointed at a
``tmp_path``.
"""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING

import httpx

from sanctum_cli.commands import screen_time as st

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ── _fetch_bridge_json: HTTP transport contract ───────────────────────


class _Recorder:
    """Records the request that crossed the bridge boundary; scripts a response.

    Installed as the handler of an ``httpx.MockTransport`` on the provider's
    ``_bridge_transport`` seam, so the REAL httpx URL-construction/encoding runs
    and the captured ``url``/``headers``/``timeout`` are exactly what would hit the
    wire (the contract that crosses the boundary — not the now-internal call shape
    the engine uses to reach it). ``timeout`` is read off the transport-level
    ``request.extensions`` httpx populates from the client timeout.
    """

    def __init__(self, *, status: int | None, body: object) -> None:
        self.status = status
        self.body = body
        self.url: str | None = None
        self.headers: dict[str, str] | None = None
        self.timeout: object = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.url = str(request.url)
        # Re-derive the single Authorization header the engine set (case-insensitive
        # on the wire; assert against the same shape the prior characterization did).
        auth = request.headers.get("authorization")
        self.headers = {"Authorization": auth} if auth is not None else None
        self.timeout = request.extensions.get("timeout", {}).get("connect")
        if self.status is None:
            raise httpx.ConnectError("simulated unreachable")
        content = _json.dumps(self.body).encode() if self.body is not None else b"not json"
        return httpx.Response(self.status, request=request, content=content)


def _install(monkeypatch: pytest.MonkeyPatch, *, status: int | None, body: object) -> _Recorder:
    rec = _Recorder(status=status, body=body)
    # Drive the REAL httpx path; intercept only the socket via MockTransport.
    monkeypatch.setattr(
        "sanctum_cli.devices.firewalla._bridge_transport",
        lambda: httpx.MockTransport(rec),
    )
    # Provide a token via env so the real ~/.sanctum token file is never read.
    monkeypatch.setenv(st._BRIDGE_TOKEN_ENV, "tok-from-env")
    return rec


class TestFetchBridgeJson:
    def test_default_url_and_bearer_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(st._BRIDGE_URL_ENV, raising=False)
        rec = _install(monkeypatch, status=200, body={"ok": True})
        out = st._fetch_bridge_json("/info")
        assert out == {"ok": True}
        # Default base URL + path concatenation is the current contract.
        assert rec.url == "http://127.0.0.1:1984/info"
        assert rec.headers == {"Authorization": "Bearer tok-from-env"}
        assert rec.timeout == 15

    def test_url_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(st._BRIDGE_URL_ENV, "http://10.0.0.9:9999")
        rec = _install(monkeypatch, status=200, body={"x": 1})
        st._fetch_bridge_json("/policies")
        assert rec.url == "http://10.0.0.9:9999/policies"

    def test_non_200_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, status=503, body={"error": "down"})
        assert st._fetch_bridge_json("/info") is None

    def test_auth_rejected_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, status=401, body={"error": "unauthorized"})
        assert st._fetch_bridge_json("/info") is None

    def test_non_json_body_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, status=200, body=None)  # 200 but body is "not json"
        assert st._fetch_bridge_json("/info") is None

    def test_non_dict_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A JSON list is valid JSON but not a mapping — current code returns None.
        _install(monkeypatch, status=200, body=[1, 2, 3])
        assert st._fetch_bridge_json("/hosts") is None

    def test_transport_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, status=None, body=None)  # ConnectError
        assert st._fetch_bridge_json("/info") is None

    def test_no_token_anywhere_returns_none_without_probe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No env token AND a token file that does not exist → fail-soft None,
        # and NO request must reach the transport (no probe without a token).
        monkeypatch.delenv(st._BRIDGE_TOKEN_ENV, raising=False)
        monkeypatch.setattr(st, "_BRIDGE_TOKEN_FILE", tmp_path / "missing-token")
        called = False

        def _boom(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            raise AssertionError("the bridge transport must not be hit without a token")

        monkeypatch.setattr(
            "sanctum_cli.devices.firewalla._bridge_transport",
            lambda: httpx.MockTransport(_boom),
        )
        assert st._fetch_bridge_json("/info") is None
        assert called is False

    def test_token_file_fallback_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No env token, but a token file exists → its (stripped) contents are
        # used as the bearer. Never reads the real ~/.sanctum file.
        monkeypatch.delenv(st._BRIDGE_TOKEN_ENV, raising=False)
        monkeypatch.delenv(st._BRIDGE_URL_ENV, raising=False)
        tf = tmp_path / "fw-token"
        tf.write_text("file-token\n", encoding="utf-8")
        monkeypatch.setattr(st, "_BRIDGE_TOKEN_FILE", tf)
        rec = _Recorder(status=200, body={"ok": 1})
        monkeypatch.setattr(
            "sanctum_cli.devices.firewalla._bridge_transport",
            lambda: httpx.MockTransport(rec),
        )
        out = st._fetch_bridge_json("/info")
        assert out == {"ok": 1}
        assert rec.headers == {"Authorization": "Bearer file-token"}


# ── _managed_macs: harvest contract ───────────────────────────────────


class TestManagedMacs:
    def test_harvests_family_shared_and_screens_uppercased_sorted(self) -> None:
        config = {
            "family": {
                "kidA": {
                    "role": "child",
                    "personal_devices": [
                        {"name": "phone", "mac": "aa:bb:cc:00:00:01"},
                        {"name": "ipad", "mac": "AA:BB:CC:00:00:02"},
                    ],
                },
                "parent1": {
                    "role": "parent",
                    "personal_devices": [{"name": "p", "mac": "aa:bb:cc:00:00:09"}],
                },
            },
            "shared_devices": {
                "tv": {"mac": "aa:bb:cc:00:00:10"},
            },
            "screens": {
                "playroom": {"macs": ["aa:bb:cc:00:00:20", "AA:BB:CC:00:00:21"]},
            },
        }
        macs = st._managed_macs(config)
        assert macs == [
            "AA:BB:CC:00:00:01",
            "AA:BB:CC:00:00:02",
            "AA:BB:CC:00:00:09",
            "AA:BB:CC:00:00:10",
            "AA:BB:CC:00:00:20",
            "AA:BB:CC:00:00:21",
        ]

    def test_empty_config_yields_no_macs(self) -> None:
        assert st._managed_macs({}) == []

    def test_devices_without_mac_are_skipped(self) -> None:
        config = {
            "family": {
                "kidA": {
                    "role": "child",
                    "personal_devices": [{"name": "no-mac-device"}],
                }
            }
        }
        assert st._managed_macs(config) == []


# ── compat_command: spoof-mode /host/<mac> monitoring loop ────────────


class TestCompatCommandMonitoringLoop:
    def test_spoof_mode_fetches_per_host_monitored_and_builds_map(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """In a non-in-path (spoof) mode, compat_command reads each managed
        MAC's `/host/<mac>` monitored flag and feeds the assembled map to
        assess_compat. Pin this end-to-end with a captured map.
        """
        import yaml

        cfg = {
            "family": {
                "kidA": {
                    "role": "child",
                    "personal_devices": [
                        {"name": "phone", "mac": "AA:BB:CC:00:00:01"},
                        {"name": "ipad", "mac": "AA:BB:CC:00:00:02"},
                    ],
                }
            }
        }
        dev = tmp_path / "devices.yaml"
        dev.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        monkeypatch.setenv("SANCTUM_DEVICES_FILE", str(dev))

        def fake_fetch(path: str) -> dict | None:
            if path == "/info":
                return {
                    "box": {"model": "red", "modelDisplay": "Red", "mode": "spoof"},
                    "capabilities": {
                        "enforcement_ready": True,
                        "box_mode": "spoof",
                    },
                }
            if path.startswith("/policies"):
                return {"policies": [], "count": 10}
            if path == "/host/AA:BB:CC:00:00:01":
                return {"monitored": True}
            if path == "/host/AA:BB:CC:00:00:02":
                return {"monitored": False}
            raise AssertionError(f"unexpected fetch {path}")

        captured: dict[str, object] = {}
        real_assess = st.assess_compat

        def spy_assess(info: dict, policy_count: int | None, monitored: object) -> list:
            captured["monitored"] = monitored
            return real_assess(info, policy_count, monitored)  # type: ignore[arg-type]

        monkeypatch.setattr(st, "_fetch_bridge_json", fake_fetch)
        monkeypatch.setattr(st, "assess_compat", spy_assess)

        # An unmonitored kid device in spoof mode is a FAIL → LocalError.
        import pytest

        from sanctum_cli.errors import LocalError

        with pytest.raises(LocalError):
            st.compat_command()

        assert captured["monitored"] == {
            "AA:BB:CC:00:00:01": True,
            "AA:BB:CC:00:00:02": False,
        }

    def test_in_path_mode_skips_per_host_monitoring_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Router/dhcp mode must NOT issue any `/host/<mac>` probe — monitoring
        is structurally guaranteed in-path, and assess_compat receives None.
        """
        fetched_paths: list[str] = []

        def fake_fetch(path: str) -> dict | None:
            fetched_paths.append(path)
            if path == "/info":
                return {
                    "box": {"model": "goldpro", "modelDisplay": "Goldpro", "mode": "router"},
                    "capabilities": {
                        "enforcement_ready": True,
                        "box_mode": "router",
                    },
                }
            if path.startswith("/policies"):
                return {"policies": [], "count": 25}
            raise AssertionError(f"unexpected fetch {path}")

        captured: dict[str, object] = {}
        real_assess = st.assess_compat

        def spy_assess(info: dict, policy_count: int | None, monitored: object) -> list:
            captured["monitored"] = monitored
            return real_assess(info, policy_count, monitored)  # type: ignore[arg-type]

        monkeypatch.setattr(st, "_fetch_bridge_json", fake_fetch)
        monkeypatch.setattr(st, "assess_compat", spy_assess)

        st.compat_command()  # all PASS → no raise

        assert captured["monitored"] is None
        assert not any(p.startswith("/host/") for p in fetched_paths)
