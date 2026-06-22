"""Sagemcom hub provider — mocked SAH client + mocked keychain, no network.

The provider drives a Sagemcom F@st hub through the ``sagemcom_api`` SAH
transport, whose every method is a coroutine. These tests mock the client
factory (``_make_client``) so no socket is ever opened, but they deliberately
do NOT mock the provider's own async-wrapping seam (``_run``/``asyncio.run``):
the fake client exposes real ``async def`` methods so the coroutine plumbing
the bug could live in is exercised for real.

Keychain is mocked too (``keychain.read`` → ``"pw"``) so no Keychain prompt
fires. Nothing here touches the live Bell hub; the live read-only smoke is a
separate, env-gated test (Task 7).
"""

from __future__ import annotations

import pytest

from sanctum_cli.devices.base import Capability, Creds, DeviceError

BRIDGE_PATH = "Device/Services/BellNetworkCfg/SetBridgeMode"


class FakeSahClient:
    """Stand-in for ``sagemcom_api.client.SagemcomClient``.

    Stores an xpath→value map and exposes the exact async surface the provider
    calls: ``login``/``logout``/``close`` plus ``get_value_by_xpath`` and
    ``set_value_by_xpath``. ``get_value_by_xpath`` returns ``None`` for unknown
    paths, mirroring the real client's best-effort leaf read. ``set_calls``
    records every (xpath, value) pair so boundary/encoding tests can assert on
    exactly what the transport received.
    """

    def __init__(self, values: dict[str, str | None]) -> None:
        self._v: dict[str, str | None] = dict(values)
        self.logged_in = False
        self.closed = False
        self.set_calls: list[tuple[str, str]] = []

    async def login(self) -> None:
        self.logged_in = True

    async def logout(self) -> None:  # pragma: no cover - not exercised everywhere
        self.logged_in = False

    async def close(self) -> None:
        self.closed = True

    async def get_value_by_xpath(self, xpath: str, options: dict | None = None) -> str | None:
        return self._v.get(xpath)

    async def set_value_by_xpath(
        self, xpath: str, value: str, options: dict | None = None
    ) -> dict:
        self.set_calls.append((xpath, value))
        self._v[xpath] = value
        return {"reply": {"error": {"description": "XMO_NO_ERR"}}}


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> FakeSahClient:
    """A connected provider's fake client, with keychain + factory mocked."""
    fake = FakeSahClient({BRIDGE_PATH: "off"})
    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    return fake


def _connected(monkeypatch: pytest.MonkeyPatch, fake: FakeSahClient):
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
    return p


def test_sagemcom_get_set_snapshot(patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _connected(monkeypatch, patched)
    snap = p.snapshot()
    assert p.set(BRIDGE_PATH, "on").after == "on"
    assert p.get(BRIDGE_PATH) == "on"
    p.rollback(snap)
    assert p.get(BRIDGE_PATH) == "off"


def test_connect_logs_in_via_keychain(patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _connected(monkeypatch, patched)
    assert patched.logged_in is True
    # provider should be usable post-connect
    assert p.get(BRIDGE_PATH) == "off"


def test_connect_reads_password_from_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect() must read the admin pw from the documented keychain tuple."""
    seen: dict[str, str] = {}

    def fake_read(account: str, service: str) -> str:
        seen["account"] = account
        seen["service"] = service
        return "pw"

    fake = FakeSahClient({BRIDGE_PATH: "off"})
    captured: dict[str, Creds] = {}

    def fake_make_client(creds: Creds) -> FakeSahClient:
        captured["creds"] = creds
        return fake

    monkeypatch.setattr("sanctum_cli.keychain.read", fake_read)
    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", fake_make_client)

    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
    assert seen == {"account": "admin", "service": "bell-hub-admin"}
    # The secret read from keychain must reach the client factory.
    assert captured["creds"].secret == "pw"


def test_get_returns_none_for_unknown_path(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    assert p.get("Device/DoesNotExist") is None


def test_set_records_opresult_before_after(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    res = p.set(BRIDGE_PATH, "on")
    assert res.ok is True
    assert res.before == "off"
    assert res.after == "on"


def test_snapshot_captures_bellnetworkcfg(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    snap = p.snapshot()
    assert snap.brand.startswith("sagemcom")
    assert snap.data[BRIDGE_PATH] == "off"
    assert snap.taken_at  # ISO-8601 stamp present


def test_rollback_resets_only_changed_keys(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    snap = p.snapshot()
    p.set(BRIDGE_PATH, "on")
    patched.set_calls.clear()
    p.rollback(snap)
    # rollback re-issues a setValue restoring the captured value
    assert (BRIDGE_PATH, "off") in patched.set_calls


def test_capabilities_advertise_hub_surface(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    caps = p.capabilities()
    assert Capability.READ in caps
    assert Capability.SET in caps
    assert Capability.BRIDGE_MODE in caps
    assert Capability.DMZ in caps
    assert Capability.WAN_MODE in caps


def test_op_before_connect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Using the provider before connect() must fail legibly, not AttributeError."""
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    with pytest.raises(DeviceError):
        p.get(BRIDGE_PATH)


def test_detect_returns_one_when_sagemcom(monkeypatch: pytest.MonkeyPatch) -> None:
    """detect() probes the gateway read-only; Sagemcom shape → ~1.0."""
    from sanctum_cli.devices import sagemcom
    from sanctum_cli.devices.base import NetContext

    # Probe is mocked so no socket opens; a Sagemcom-shaped reply scores 1.0.
    monkeypatch.setattr(sagemcom, "_probe_is_sagemcom", lambda gateway_ip: True)
    net = NetContext(gateway_ip="192.168.2.1", runner=None)
    assert sagemcom.SagemcomHubProvider.detect(net) == 1.0


def test_detect_zero_when_no_gateway() -> None:
    from sanctum_cli.devices import sagemcom
    from sanctum_cli.devices.base import NetContext

    net = NetContext(gateway_ip=None, runner=None)
    assert sagemcom.SagemcomHubProvider.detect(net) == 0.0


def test_detect_zero_when_not_sagemcom(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import sagemcom
    from sanctum_cli.devices.base import NetContext

    monkeypatch.setattr(sagemcom, "_probe_is_sagemcom", lambda gateway_ip: False)
    net = NetContext(gateway_ip="10.0.0.1", runner=None)
    assert sagemcom.SagemcomHubProvider.detect(net) == 0.0


def test_provider_registered_under_hub() -> None:
    """Importing the module self-registers the provider under kind=hub."""
    from sanctum_cli.devices import registry, sagemcom

    assert "hub" in registry._REGISTRY
    assert sagemcom.SagemcomHubProvider in registry._REGISTRY["hub"]
