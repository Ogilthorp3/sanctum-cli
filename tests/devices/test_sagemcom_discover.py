"""Sagemcom read-only subtree discovery — mocked client, no network.

``discover(path, depth)`` is the read-only capability-mapping engine: it issues a
single SAH ``getValue`` at ``path`` (NEVER ``setValue`` — discovery must not
mutate), walks the returned datamodel subtree, and reports each leaf's value plus
whether it is *settable* on this hub. Writability is derived from the SAH leaf
``flags`` metadata: a leaf is a WALL when its flags carry ``NON_WRITABLE``
(firmware) or ``ACCESS_RESTRICTION`` (Bell-locked) — the two wall classes the
audit found on the F5697; everything else is settable through the near-total
``setValue`` surface.

The fake here is authored from the LIBRARY'S ACTUAL reply navigation, not a
convenient shape the production code hopes for (Contracts at the Boundary — a
test cannot catch a bug it shares). The installed ``sagemcom_api`` extracts a
getValue's payload at ``reply["reply"]["actions"][0]["callbacks"][0]
["parameters"]["value"]`` (see ``SagemcomClient.__get_response`` /
``__get_response_value``), so the fake's raw seam returns EXACTLY that envelope
shape with a nested datamodel subtree as the ``value``.

Crucially the production code must walk the RAW (un-decamelized) subtree: the
library's ``get_value_by_xpath`` runs ``humps.decamelize`` over the value, which
would rewrite ``WiFi`` → ``wi_fi`` and mangle every datamodel path. The
PascalCase path assertions below FAIL if ``discover`` were (wrongly) built on the
decamelizing wrapper instead of the raw ``__api_request_async`` seam.
"""

from __future__ import annotations

import asyncio

import pytest

from sanctum_cli.devices.base import Creds, DeviceError

# A realistic SAH attribute-form getValue subtree: each parameter leaf is a dict
# carrying a ``value`` and a ``flags`` token string; a non-leaf is a dict of
# child objects. The two walls mirror the audit: firmware NON_WRITABLE + Bell
# ACCESS_RESTRICTION; everything else is settable.
_SUBTREE = {
    "WiFi": {
        "SSID": {
            "1": {
                "Enable": {"value": "true", "flags": "writable persistent"},
                "SSID": {"value": "BellNet", "flags": "writable"},
            }
        },
        "Radio": {"1": {"Channel": {"value": "6", "flags": "writable"}}},
    },
    # A SHALLOW leaf (one object level below Device) so a depth-bounded walk can
    # be proven: at depth 2 this is reached but the deeper WiFi leaves are not.
    "DeviceInfo": {
        "SoftwareVersion": {"value": "3.11.6.1", "flags": "read_only NON_WRITABLE"},
    },
    "Services": {
        "BellNetworkCfg": {
            "SetBridgeMode": {"value": "off", "flags": "ACCESS_RESTRICTION"},
        }
    },
}


def _getvalue_envelope(subtree: dict) -> dict:
    """The exact reply shape the library's ``__get_response`` navigates.

    Mirrors ``reply["reply"]["actions"][0]["callbacks"][0]["parameters"]["value"]``
    — derived from the installed ``sagemcom_api`` source, NOT from the production
    code's assumption, so the test pins the real boundary contract.
    """
    return {
        "reply": {
            "error": {"description": "XMO_NO_ERR"},
            "actions": [
                {
                    "error": {"description": "XMO_NO_ERR"},
                    "callbacks": [{"parameters": {"value": subtree}}],
                }
            ],
        }
    }


class RecordingDiscoverClient:
    """Stand-in for ``SagemcomClient`` that records raw-seam getValue actions.

    Mirrors ``RecordingActionClient`` (loop-binding fidelity + a configurable raw
    seam), but its raw seam returns a getValue SUBTREE envelope and records every
    action issued — so the test asserts the exact SAH verb/xpath/options that
    would hit the wire. It deliberately exposes NO ``set_value_by_xpath``: if
    ``discover`` tried to mutate, it would ``AttributeError`` here, making that
    contract violation observable.
    """

    def __init__(self, subtree: dict | None = None) -> None:
        self.raw_actions: list = []
        self._subtree = subtree if subtree is not None else _SUBTREE
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

    async def logout(self) -> None:
        self._assert_same_loop()

    async def close(self) -> None:
        self._assert_same_loop()

    async def get_value_by_xpath(self, xpath: str, options: dict | None = None) -> str | None:
        # Brand-refine read on connect lands here (NOT the raw seam), so it does
        # not pollute ``raw_actions``. Unknown path → None (best-effort).
        self._assert_same_loop()
        return None

    async def _SagemcomClient__api_request_async(  # noqa: N802 - mirror the lib's name-mangled raw seam
        self, actions: list, priority: bool = False
    ) -> dict:
        self._assert_same_loop()
        self.raw_actions.append(actions)
        return _getvalue_envelope(self._subtree)


class MissingRawSeamClient:
    """A client WITHOUT the name-mangled raw seam (a hypothetical lib rename).

    ``discover`` walks the RAW envelope, so the only path that returns it is the
    raw seam — there is no convenience fallback that preserves PascalCase keys.
    The provider MUST raise rather than silently degrade to the decamelizing
    wrapper (which would mangle every path).
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


def _by_path(leaves: list) -> dict:
    return {leaf.path: leaf for leaf in leaves}


# ── discover(): the read-only subtree walk ──────────────────────────────────


def test_discover_walks_subtree_returning_leaves_with_writability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """discover() returns every datamodel leaf annotated with its writability.

    PascalCase paths are preserved (proof the walk is over the RAW envelope, not
    the decamelizing ``get_value_by_xpath`` — that would yield ``device/wi_fi/..``).
    The two walls (firmware NON_WRITABLE + Bell ACCESS_RESTRICTION) are reported
    writable=False with their restriction token; every other leaf is settable.
    """
    fake = RecordingDiscoverClient()
    p = _connected(monkeypatch, fake)
    leaves = p.discover(path="Device", depth=6)
    by_path = _by_path(leaves)

    # Settable WiFi leaves, PascalCase preserved (NOT decamelized).
    enable = by_path["Device/WiFi/SSID/1/Enable"]
    assert enable.writable is True
    assert enable.restriction is None
    assert enable.value == "true"
    assert by_path["Device/WiFi/SSID/1/SSID"].value == "BellNet"
    assert by_path["Device/WiFi/Radio/1/Channel"].writable is True

    # Walls: firmware is NON_WRITABLE, Bell leaf is ACCESS_RESTRICTION.
    fw = by_path["Device/DeviceInfo/SoftwareVersion"]
    assert fw.writable is False
    assert fw.restriction == "NON_WRITABLE"
    bell = by_path["Device/Services/BellNetworkCfg/SetBridgeMode"]
    assert bell.writable is False
    assert bell.restriction == "ACCESS_RESTRICTION"

    # The settable subset excludes exactly the two walls.
    settable = {leaf.path for leaf in leaves if leaf.writable}
    assert "Device/WiFi/SSID/1/Enable" in settable
    assert "Device/DeviceInfo/SoftwareVersion" not in settable
    assert "Device/Services/BellNetworkCfg/SetBridgeMode" not in settable


def test_discover_issues_read_only_getvalue_with_depth_through_raw_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """discover() fires a single ``getValue`` (never ``setValue``) carrying depth.

    The recorded raw action MUST be the SAH getValue shape at the requested xpath,
    with the depth threaded into the options — issued once on the persistent loop
    login bound to. discover is READ-ONLY: no setValue verb is ever built.
    """
    fake = RecordingDiscoverClient()
    p = _connected(monkeypatch, fake)
    p.discover(path="Device/WiFi", depth=3)

    assert len(fake.raw_actions) == 1
    issued = fake.raw_actions[0]
    assert isinstance(issued, list) and len(issued) == 1
    action = issued[0]
    assert action["method"] == "getValue"
    assert action["xpath"] == "Device/WiFi"
    assert action["options"]["depth"] == 3
    # Read-only: not a single setValue verb anywhere in what was issued.
    assert all(a["method"] != "setValue" for batch in fake.raw_actions for a in batch)
    # Same still-open loop login bound to (no per-call asyncio.run).
    assert fake._bound_loop is not None and fake._bound_loop.is_closed() is False


def test_discover_depth_bounds_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """``depth`` provably limits how deep the subtree walk descends.

    At depth 2 the shallow ``Device/DeviceInfo/SoftwareVersion`` leaf (one object
    level below Device) is reached, but the deeper ``Device/WiFi/SSID/1/Enable``
    (four levels down) is NOT — a real, verifiable effect of the bound that does
    not rely on the firmware honoring the request-side option.
    """
    fake = RecordingDiscoverClient()
    p = _connected(monkeypatch, fake)
    paths = {leaf.path for leaf in p.discover(path="Device", depth=2)}
    assert "Device/DeviceInfo/SoftwareVersion" in paths
    assert "Device/WiFi/SSID/1/Enable" not in paths


def test_discover_empty_subtree_returns_no_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply with an empty / non-dict value yields an empty leaf list, not a crash."""
    fake = RecordingDiscoverClient(subtree={})
    p = _connected(monkeypatch, fake)
    assert p.discover(path="Device", depth=3) == []


def test_discover_before_connect_raises() -> None:
    """discover() before connect() must fail legibly, not AttributeError."""
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    with pytest.raises(DeviceError):
        p.discover(path="Device", depth=1)


def test_discover_fails_closed_when_raw_seam_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No raw seam → DeviceError: never silently degrade to the decamelizing wrapper.

    Walking the decamelized ``get_value_by_xpath`` value would mangle every path
    (``WiFi`` → ``wi_fi``), so there is no safe fallback. The provider MUST raise,
    matching ``action()``'s missing-seam contract.
    """
    fake = MissingRawSeamClient()
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.discover(path="Device", depth=3)
