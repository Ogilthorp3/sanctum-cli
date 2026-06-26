"""Sagemcom generic SAH verb + table-row ops — mocked client, no network.

``set()`` reaches a single *leaf* by XPath, but the one class of Bell config it
cannot touch is a multi-instance *table*: port-forwards, DHCP static leases,
firewall rules. Those are mutated with the SAH object verbs ``addChild`` /
``deleteChild`` (+ ``applyChanges`` to commit), and the generic escape hatch
``action(method, xpath, parameters)`` reaches the full verb set the raw
transport seam exposes. The ``sagemcom_api`` 1.4.3 client ships NO convenience
wrapper for any of these (only ``get_value_by_xpath`` / ``set_value_by_xpath`` /
``reboot``), so they are issued through the same name-mangled raw request seam
``reboot()`` uses (``_SagemcomClient__api_request_async``) — which returns the
full ``{"reply": {"error": ...}}`` envelope the fail-closed inspector reads.

These tests drive the new methods through the provider's REAL async-wrapping
seam (``_run`` / the persistent event loop) against a recording fake whose
surface is authored from the *library's actual behaviour* (Contracts at the
Boundary: a test cannot catch a bug it shares):

* the raw seam returns a CONFIGURABLE full reply envelope and records the exact
  ``actions`` list it received (so we assert the SAH verb shape that would hit
  the wire, not a field a convenient fake recorded before serialization);
* a clean envelope (``XMO_NO_ERR`` / ``XMO_REQUEST_NO_ERR``) → ``OpResult(ok=True)``;
* an envelope the transport RETURNS (not raises) on an error description it does
  not model (``XMO_*_ERR``, top-level OR per-action) → ``DeviceError``. The real
  ``__post`` only raises for the handful of descriptions it models and RETURNS
  for the rest, so "the call did not raise" is NOT proof the verb landed.

A separate, env-gated module (``test_sagemcom_boundary.py``) proves the xpath
encoding through the GENUINE ``sagemcom_api`` transport; here the network is
fully mocked.
"""

from __future__ import annotations

import asyncio

import pytest

from sanctum_cli.devices.base import Creds, DeviceError

PORTMAP_TABLE = "Device/NAT/PortMapping"
STATIC_LEASE_TABLE = "Device/DHCPv4/Server/Pool/StaticAddress"


class RecordingActionClient:
    """Stand-in for ``SagemcomClient`` that records raw-seam actions.

    Mirrors ``test_reboot.RecordingRebootClient``: it carries the loop-binding
    fidelity of the real ``aiohttp.ClientSession`` (bound to the loop it is first
    driven in, so a per-call ``asyncio.run`` regression surfaces as
    ``RuntimeError: Event loop is closed``), and its raw request seam returns a
    CONFIGURABLE full reply envelope — exactly what the provider must inspect —
    while recording every ``actions`` list it received.

    It deliberately exposes NO ``addChild`` / ``deleteChild`` / ``applyChanges``
    convenience method, because the real ``sagemcom_api`` client has none: the
    ONLY path for these verbs is the raw seam. If the provider tried to call a
    non-existent wrapper, it would ``AttributeError`` here — making that wrong
    path observable.
    """

    def __init__(self, reply: dict | None = None) -> None:
        self.logged_in = False
        self.closed = False
        self.logged_out = False
        self.raw_actions: list = []
        self._reply = reply if reply is not None else {
            "reply": {"error": {"description": "XMO_NO_ERR"}}
        }
        self._bound_loop: asyncio.AbstractEventLoop | None = None

    def _assert_same_loop(self) -> None:
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

    async def close(self) -> None:
        self._assert_same_loop()
        self.closed = True

    async def get_value_by_xpath(self, xpath: str, options: dict | None = None) -> str | None:
        # Brand-refine read on connect lands here, NOT on the raw seam, so it does
        # not pollute ``raw_actions``. Unknown path → None (best-effort).
        self._assert_same_loop()
        return None

    async def _SagemcomClient__api_request_async(  # noqa: N802 - must mirror the lib's name-mangled raw seam
        self, actions: list, priority: bool = False
    ) -> dict:
        """The RAW request seam: records the actions, returns the full envelope."""
        self._assert_same_loop()
        self.raw_actions.append(actions)
        return self._reply


class MissingRawSeamClient:
    """A client WITHOUT the name-mangled raw seam (a hypothetical lib rename).

    The generic verbs have NO convenience-wrapper fallback (unlike ``reboot``),
    so if the raw seam disappears there is no safe path that returns the
    fail-closed envelope. The provider MUST raise rather than silently degrade.
    """

    def __init__(self) -> None:
        self._bound_loop: asyncio.AbstractEventLoop | None = None

    def _assert_same_loop(self) -> None:
        current = asyncio.get_running_loop()
        if self._bound_loop is None:
            self._bound_loop = current

    async def login(self) -> None:
        self._assert_same_loop()

    async def logout(self) -> None:
        self._assert_same_loop()

    async def close(self) -> None:
        self._assert_same_loop()

    async def get_value_by_xpath(self, xpath: str, options: dict | None = None) -> str | None:
        self._assert_same_loop()
        return None


_OPENED: list = []


@pytest.fixture(autouse=True)
def _disconnect_opened():
    """Disconnect every provider opened during the test (closes its loop)."""
    _OPENED.clear()
    yield
    while _OPENED:
        _OPENED.pop().disconnect()


def _connected(monkeypatch: pytest.MonkeyPatch, fake: object):
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    p = SagemcomHubProvider()
    p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
    _OPENED.append(p)
    return p


# ── action(): the generic SAH verb escape hatch ─────────────────────────────


def test_action_issues_arbitrary_sah_verb_through_raw_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """action() builds the SAH action and issues it once through the raw seam.

    The action dict that reaches the (recording) raw seam MUST carry the verb,
    xpath and parameters verbatim in the library's action shape
    (``{"id", "method", "xpath", "parameters"}``), fired once on the persistent
    loop login bound to. A clean reply yields a successful OpResult.
    """
    fake = RecordingActionClient()
    p = _connected(monkeypatch, fake)
    res = p.action("getParameterNames", "Device/NAT", {"flag": "x"})
    assert len(fake.raw_actions) == 1
    issued = fake.raw_actions[0]
    assert isinstance(issued, list) and len(issued) == 1
    action = issued[0]
    assert action["method"] == "getParameterNames"
    assert action["xpath"] == "Device/NAT"
    assert action["parameters"] == {"flag": "x"}
    # Same still-open loop login bound to (no per-call asyncio.run).
    assert fake._bound_loop is not None and fake._bound_loop.is_closed() is False
    assert res.ok is True


def test_action_defaults_parameters_to_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """action() with no parameters issues an empty ``parameters`` dict (SAH shape)."""
    fake = RecordingActionClient()
    p = _connected(monkeypatch, fake)
    p.action("applyChanges", "Device")
    action = fake.raw_actions[0][0]
    assert action["parameters"] == {}


def test_action_fails_closed_on_unmodeled_top_level_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top-level error the transport RETURNS (not raises) must fail-closed."""
    fake = RecordingActionClient(
        reply={"reply": {"error": {"description": "XMO_UNKNOWN_PATH_ERR"}}}
    )
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.action("addChild", PORTMAP_TABLE, {"x": "1"})


def test_action_fails_closed_on_per_action_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Top-level NO_ERR but a failed ACTION — the library never checks this, so
    the provider must, or a rejected table op reports a green outcome."""
    fake = RecordingActionClient(
        reply={
            "reply": {
                "error": {"description": "XMO_NO_ERR"},
                "actions": [{"error": {"description": "XMO_NON_WRITABLE_PARAMETER_ERR"}}],
            }
        }
    )
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.action("addChild", PORTMAP_TABLE, {"x": "1"})


def test_action_succeeds_on_request_no_err_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The library's own success token ``XMO_REQUEST_NO_ERR`` counts as clean too."""
    fake = RecordingActionClient(
        reply={"reply": {"error": {"description": "XMO_REQUEST_NO_ERR"}}}
    )
    p = _connected(monkeypatch, fake)
    assert p.action("applyChanges", "Device").ok is True


def test_action_before_connect_raises() -> None:
    """action() before connect() must fail legibly, not AttributeError."""
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    with pytest.raises(DeviceError):
        p.action("applyChanges", "Device")


def test_action_fails_closed_when_raw_seam_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No raw seam + no convenience fallback → DeviceError, never a silent no-op.

    Unlike ``reboot`` (which can fall back to ``client.reboot()``), the generic
    verbs have no library wrapper, so a missing/renamed raw seam leaves no safe
    path that returns the fail-closed envelope. The provider MUST raise.
    """
    fake = MissingRawSeamClient()
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.action("addChild", PORTMAP_TABLE, {"x": "1"})


# ── add_row(): addChild for a table setting ─────────────────────────────────


def test_add_row_issues_addchild_with_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """add_row() issues an ``addChild`` against the table xpath with the params."""
    fake = RecordingActionClient()
    p = _connected(monkeypatch, fake)
    params = {"ExternalPort": "8443", "InternalPort": "443", "Protocol": "TCP"}
    res = p.add_row(PORTMAP_TABLE, params)
    action = fake.raw_actions[0][0]
    assert action["method"] == "addChild"
    assert action["xpath"] == PORTMAP_TABLE
    assert action["parameters"] == params
    assert res.ok is True


def test_add_row_fails_closed_on_rejected_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected addChild reply the transport RETURNS must fail-closed."""
    fake = RecordingActionClient(
        reply={"reply": {"error": {"description": "XMO_ACCESS_RESTRICTION_ERR"}}}
    )
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.add_row(PORTMAP_TABLE, {"ExternalPort": "8443"})
    # Proof it really issued the addChild verb before failing closed.
    assert fake.raw_actions[0][0]["method"] == "addChild"


# ── delete_row(): deleteChild for a table setting ───────────────────────────


def test_delete_row_issues_deletechild_with_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_row() issues a ``deleteChild`` carrying the instance index."""
    fake = RecordingActionClient()
    p = _connected(monkeypatch, fake)
    res = p.delete_row(STATIC_LEASE_TABLE, 3)
    action = fake.raw_actions[0][0]
    assert action["method"] == "deleteChild"
    assert action["xpath"] == STATIC_LEASE_TABLE
    assert action["parameters"] == {"index": 3}
    assert res.ok is True


def test_delete_row_fails_closed_on_rejected_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected deleteChild reply must fail-closed, not report a green delete."""
    fake = RecordingActionClient(
        reply={
            "reply": {
                "error": {"description": "XMO_NO_ERR"},
                "actions": [{"error": {"description": "XMO_UNKNOWN_PATH_ERR"}}],
            }
        }
    )
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.delete_row(STATIC_LEASE_TABLE, 99)


# ── apply_changes(): commit the pending transaction ─────────────────────────


def test_apply_changes_issues_applychanges(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_changes() issues the SAH ``applyChanges`` verb, fail-closed on reject."""
    fake = RecordingActionClient()
    p = _connected(monkeypatch, fake)
    res = p.apply_changes()
    action = fake.raw_actions[0][0]
    assert action["method"] == "applyChanges"
    assert res.ok is True


def test_apply_changes_fails_closed_on_rejected_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected applyChanges reply must fail-closed."""
    fake = RecordingActionClient(
        reply={"reply": {"error": {"description": "XMO_UNKNOWN_PATH_ERR"}}}
    )
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.apply_changes()
