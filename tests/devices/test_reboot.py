"""SagemcomHubProvider.reboot() — mocked SAH client, no network.

The Bell Home Hub exposes a ``reboot`` action over the SAH JSON-req transport
(``sagemcom_api``'s ``client.reboot()`` issues the ``{"method": "reboot",
"xpath": "Device"}`` action). These tests drive ``reboot()`` through the same
mocked-client / real-loop pattern the rest of the Sagemcom suite uses (see
``test_sagemcom.py``): the client factory is mocked so no socket opens, but the
provider's own async-wrapping seam (``_run`` / the persistent event loop) is
exercised for real against a fake exposing genuine ``async def`` methods.

Reboot is fail-closed for the SAME reason ``set`` is (Contracts at the
Boundary): the ``sagemcom_api`` transport RETURNS (does not raise) on an error
description it does not model, so "the call did not raise" is NOT proof the hub
accepted the reboot. The recording fake therefore returns a full SAH reply
envelope (``{"reply": {"error": {"description": ...}}}``) — the exact shape the
provider's existing ``_reply_error`` inspects — and the hostile cases below are
built from the library's REAL reply schema, not from the production code's
assumption.
"""

from __future__ import annotations

import pytest

from sanctum_cli.devices.base import Capability, Creds, DeviceError

BRIDGE_PATH = "Device/Services/BellNetworkCfg/SetBridgeMode"


class RecordingRebootClient:
    """Stand-in for ``sagemcom_api.client.SagemcomClient`` that records reboot.

    Reuses the loop-binding fidelity of ``test_sagemcom.FakeSahClient`` (the real
    client's ``aiohttp.ClientSession`` binds to the loop it is first driven in, so
    a per-call ``asyncio.run`` would break the first op after ``connect``). Each
    coroutine asserts it is driven on the SAME, still-open loop login bound to —
    raising ``RuntimeError`` exactly as aiohttp would on a regression.

    Crucially, this fake's surface is authored from the *library's actual
    behaviour*, not the production code's hopes (CLAUDE.md: a test cannot catch a
    bug it shares). The real ``SagemcomClient.reboot()`` returns
    ``__get_response_value(response)`` — the extracted leaf value, NOT the reply
    envelope (and for the reboot action, ``''`` for both clean and rejected
    replies). The envelope only survives on the client's RAW request path
    (name-mangled ``_SagemcomClient__api_request_async``), which returns
    ``{"reply": {"error": ..., "actions": [...]}}``. So:

    * ``_SagemcomClient__api_request_async`` returns the CONFIGURABLE full
      envelope (what the provider must inspect) and records the action it received;
    * ``reboot`` mimics the library's lossy wrapper — it returns ``''`` regardless
      of success/failure — so if the provider regressed to calling it, the
      fail-closed and success cases would BOTH break, making the regression
      observable through the fake.
    """

    def __init__(self, reply: dict | None = None) -> None:
        import asyncio

        self._asyncio = asyncio
        self.logged_in = False
        self.closed = False
        self.logged_out = False
        self.reboot_calls = 0
        self.raw_actions: list = []
        self._reply = reply if reply is not None else {
            "reply": {"error": {"description": "XMO_NO_ERR"}}
        }
        self._bound_loop = None

    def _assert_same_loop(self) -> None:
        current = self._asyncio.get_running_loop()
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

    async def close(self) -> None:
        self._assert_same_loop()
        self.closed = True

    async def get_value_by_xpath(self, xpath: str, options: dict | None = None) -> str | None:
        self._assert_same_loop()
        return None

    async def _SagemcomClient__api_request_async(  # noqa: N802 - must mirror the lib's name-mangled raw seam
        self, actions: list, priority: bool = False
    ) -> dict:
        """The RAW request seam: returns the full reply envelope (like the lib)."""
        self._assert_same_loop()
        self.reboot_calls += 1
        self.raw_actions.append(actions)
        return self._reply

    async def reboot(self) -> str:
        """The library's lossy convenience wrapper: returns ``''``, not the envelope.

        If the provider regressed to calling this, ``_reply_error('')`` would
        treat a perfectly clean reboot as a failure (and could never distinguish a
        rejected one) — so this is here to make that wrong path fail the tests.
        """
        self._assert_same_loop()
        self.reboot_calls += 1
        return ""


_OPENED: list = []


@pytest.fixture(autouse=True)
def _disconnect_opened():
    """Disconnect every provider opened during the test (closes its loop)."""
    _OPENED.clear()
    yield
    while _OPENED:
        _OPENED.pop().disconnect()


def _connected(monkeypatch: pytest.MonkeyPatch, fake: RecordingRebootClient):
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    p = SagemcomHubProvider()
    p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
    _OPENED.append(p)
    return p


def test_reboot_issues_sah_reboot_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """reboot() issues the SAH ``reboot`` action on the persistent loop.

    Asserts on the exact action the recording fake received through the raw
    request seam: ``{"method": "reboot", "xpath": "Device"}`` — the SAH reboot
    action — fired once, on the same still-open loop login bound to.
    """
    fake = RecordingRebootClient()
    p = _connected(monkeypatch, fake)
    result = p.reboot()
    # The reboot action was issued exactly once...
    assert fake.reboot_calls == 1
    # ...with the SAH reboot shape (method + Device xpath)...
    assert len(fake.raw_actions) == 1
    issued = fake.raw_actions[0]
    assert isinstance(issued, list) and len(issued) == 1
    action = issued[0]
    assert action["method"] == "reboot"
    assert action["xpath"] == "Device"
    # ...on the same still-open loop login bound to (no per-call asyncio.run).
    assert fake._bound_loop is not None
    assert fake._bound_loop.is_closed() is False
    # A clean reply yields a successful OpResult.
    assert result.ok is True
    assert result.detail == "reboot issued"


def test_reboot_does_not_use_lossy_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """reboot() must NOT route through the lib's lossy ``reboot()`` wrapper.

    That wrapper returns the extracted value (``''`` for the reboot action), which
    would make ``_reply_error`` treat a clean reboot as a failure. The provider
    must use the raw request path that preserves the envelope, so a clean reboot
    succeeds (it would raise DeviceError if it went through the wrapper).
    """
    fake = RecordingRebootClient()  # clean XMO_NO_ERR envelope on the raw path
    p = _connected(monkeypatch, fake)
    res = p.reboot()
    assert res.ok is True
    # Proof it took the raw path (envelope-bearing), not the lossy wrapper.
    assert fake.raw_actions, "reboot() should issue via the raw request seam"


def test_reboot_raises_on_hostile_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply the transport RETURNS (not raises) on an unmodeled error must
    fail-closed — reboot() must not report success on a rejected reboot."""
    fake = RecordingRebootClient(
        reply={"reply": {"error": {"description": "XMO_ACCESS_RESTRICTION_ERR"}}}
    )
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.reboot()


def test_reboot_before_connect_raises() -> None:
    """reboot() before connect() must fail legibly, not AttributeError."""
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    with pytest.raises(DeviceError):
        p.reboot()


def test_reboot_capability_advertised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider still advertises Capability.REBOOT after reboot() lands."""
    fake = RecordingRebootClient()
    p = _connected(monkeypatch, fake)
    assert Capability.REBOOT in p.capabilities()
