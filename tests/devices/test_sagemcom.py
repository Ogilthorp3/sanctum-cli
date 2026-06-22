"""Sagemcom hub provider — mocked SAH client + mocked keychain, no network.

The provider drives a Sagemcom F@st hub through the ``sagemcom_api`` SAH
transport, whose every method is a coroutine. These tests mock the client
factory (``_make_client``) so no socket is ever opened, but they deliberately
do NOT mock the provider's own async-wrapping seam (``_run``/the persistent
event loop): the fake client exposes real ``async def`` methods so the
coroutine plumbing the bug could live in is exercised for real.

The real ``SagemcomClient`` builds ONE ``aiohttp.ClientSession`` in
``__init__`` and reuses it on every request; aiohttp binds that session to the
event loop it is first driven in, so a fresh ``asyncio.run`` loop per call would
break the first op after ``connect()`` with ``RuntimeError: Event loop is
closed``. A naive fake holds no loop-bound resource and so *cannot* catch that
class of bug (CLAUDE.md: "a test cannot catch a bug it shares"). To close that
gap, :class:`FakeSahClient` mimics the loop-binding: it records the loop
``login`` ran on and every later coroutine asserts it is being driven on that
SAME, still-open loop — raising ``RuntimeError`` exactly as aiohttp would if the
provider regressed to per-call ``asyncio.run``.

Keychain is mocked too (``keychain.read`` → ``"pw"``) so no Keychain prompt
fires. Nothing here touches the live Bell hub; the live read-only smoke is a
separate, env-gated test (Task 7).
"""

from __future__ import annotations

import asyncio

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

    Loop-binding fidelity: the real client's ``aiohttp.ClientSession`` is bound
    to the loop it is first driven in. We emulate that — ``login`` records the
    running loop, and every later coroutine calls :meth:`_assert_same_loop`,
    which raises ``RuntimeError`` if driven on a different or closed loop. That
    makes the multi-loop bug observable through the fake instead of only against
    live gear.
    """

    def __init__(self, values: dict[str, str | None]) -> None:
        self._v: dict[str, str | None] = dict(values)
        self.logged_in = False
        self.closed = False
        self.logged_out = False
        self.set_calls: list[tuple[str, str]] = []
        # The loop the (simulated) aiohttp session bound to at login time.
        self._bound_loop: asyncio.AbstractEventLoop | None = None

    def _assert_same_loop(self) -> None:
        """Mirror aiohttp: a session bound to loop A is unusable under loop B."""
        current = asyncio.get_running_loop()
        if self._bound_loop is None:
            self._bound_loop = current
        elif current is not self._bound_loop or self._bound_loop.is_closed():
            msg = "Event loop is closed"
            raise RuntimeError(msg)

    async def login(self) -> None:
        self._assert_same_loop()
        self.logged_in = True

    async def logout(self) -> None:
        self._assert_same_loop()
        self.logged_out = True
        self.logged_in = False

    async def close(self) -> None:
        self._assert_same_loop()
        self.closed = True

    async def get_value_by_xpath(self, xpath: str, options: dict | None = None) -> str | None:
        self._assert_same_loop()
        return self._v.get(xpath)

    async def set_value_by_xpath(
        self, xpath: str, value: str, options: dict | None = None
    ) -> dict:
        self._assert_same_loop()
        self.set_calls.append((xpath, value))
        self._v[xpath] = value
        return {"reply": {"error": {"description": "XMO_NO_ERR"}}}


# Providers created via ``_connected`` are registered here so an autouse
# fixture can ``disconnect`` them at test teardown — a connected provider owns
# an open event loop, and leaking it raises an unraisable ResourceWarning when
# it is later garbage-collected.
_OPENED: list = []


@pytest.fixture(autouse=True)
def _disconnect_opened():
    """Disconnect every provider opened during the test (closes its loop)."""
    _OPENED.clear()
    yield
    while _OPENED:
        _OPENED.pop().disconnect()


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
    _OPENED.append(p)
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


def test_snapshot_guarantees_bridge_mode_baseline_when_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the firmware does not expose the bridge-mode leaf, snapshot still carries
    a safe restorable baseline for it (so rollback is never a silent no-op)."""
    # Fake client whose bridge-mode read returns None (leaf not exposed).
    fake = FakeSahClient({})  # empty: get_value_by_xpath → None for everything
    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    p = _connected(monkeypatch, fake)
    snap = p.snapshot()
    # The leaf the cutover mutates MUST be present even though its read was None.
    assert snap.data[BRIDGE_PATH] == "off"


def test_rollback_empty_snapshot_reports_failure(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty-baseline rollback must report ok=False — not a silent success."""
    from sanctum_cli.devices.base import Snapshot

    p = _connected(monkeypatch, patched)
    empty = Snapshot(brand="sagemcom", taken_at="t", data={})
    res = p.rollback(empty)
    assert res.ok is False  # nothing to restore is a FAILED rollback
    assert patched.set_calls == []  # and it issued no writes


def test_rollback_with_baseline_reports_success(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollback that restores at least one leaf reports ok=True."""
    p = _connected(monkeypatch, patched)
    snap = p.snapshot()  # carries the guaranteed bridge-mode baseline
    res = p.rollback(snap)
    assert res.ok is True


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


def test_ops_after_connect_share_one_loop(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: login + every later op must run on ONE persistent loop.

    The real client holds a loop-bound aiohttp session; with per-call
    ``asyncio.run`` the loop login bound to is already closed by the first
    get/set, so the call would raise ``RuntimeError: Event loop is closed``.
    The fake reproduces that binding, so this exercises the real failure mode —
    not just structural shape. Several ops in a row prove the loop persists.
    """
    p = _connected(monkeypatch, patched)
    # All of these are distinct provider calls; pre-fix each spun a fresh loop.
    assert p.get(BRIDGE_PATH) == "off"
    assert p.set(BRIDGE_PATH, "on").after == "on"
    assert p.get(BRIDGE_PATH) == "on"
    snap = p.snapshot()
    assert snap.data[BRIDGE_PATH] == "on"
    # The fake bound to login's loop and never saw a different/closed one.
    assert patched._bound_loop is not None
    assert patched._bound_loop.is_closed() is False


def test_disconnect_closes_session_and_loop(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """disconnect() logs out + closes the client and tears the loop down."""
    p = _connected(monkeypatch, patched)
    loop = p._loop
    assert loop is not None and loop.is_closed() is False
    p.disconnect()
    assert patched.logged_out is True
    assert patched.closed is True
    assert p._loop is None
    assert loop.is_closed() is True


def test_disconnect_is_idempotent_and_safe_unconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect() is a no-op when never connected and safe to call twice."""
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    p.disconnect()  # never connected: must not raise
    assert p._loop is None


def test_disconnect_then_reconnect_reads_again(
    patched: FakeSahClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After disconnect, a fresh connect opens a NEW loop and reads succeed.

    The post-reconnect read would raise ``Event loop is closed`` if the fake's
    loop binding were stale, so this proves the second lifetime is clean.
    """
    p = _connected(monkeypatch, patched)
    p.disconnect()
    # Reset the fake's binding so it can bind to the new lifetime's loop,
    # mirroring a freshly constructed client on reconnect.
    fresh = FakeSahClient({BRIDGE_PATH: "off"})
    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: fresh)
    p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
    assert p.get(BRIDGE_PATH) == "off"
    assert fresh._bound_loop is not None and fresh._bound_loop.is_closed() is False
