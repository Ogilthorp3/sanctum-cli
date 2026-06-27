"""SAH-boundary encoding contract + env-gated live read-only smoke (Task 7).

Two concerns live here, both about the *boundary* between the provider and the
real ``sagemcom_api`` transport — the seam where a caller-supplied path/value
crosses into the SAH ``json-req`` request.

1. **Hostile-input boundary (Step 1).** CLAUDE.md "Own the escaping at the
   boundary; test the hostile input, not the happy path": a path/value carrying
   a literal ``%``, a space, AND a non-ASCII char must be encoded *correctly* for
   the SAH request — exactly once. The footgun is double-encoding: the SAH xpath
   is URL-quoted by ``sagemcom_api`` (``urllib.parse.quote``), so if the provider
   *also* quoted, a literal ``%`` would become ``%2525`` and silently address the
   wrong leaf — every normal path passes and the call dies (or misfires) on the
   one pathological row. The contract is: the provider passes path/value
   **verbatim** to the library setter/getter, and the library owns the single
   layer of encoding.

   The earlier ``test_sagemcom.py`` uses a ``FakeSahClient`` that records
   ``(xpath, value)`` *before* any encoding — so it can prove pass-through but it
   cannot prove the bytes that actually hit the wire are right (a test cannot
   catch a bug it short-circuits). So this module drives the **real**
   ``SagemcomClient`` encoding path and mocks ONLY the genuinely expensive layer
   (the network ``__post``), then asserts on the real SAH ``actions`` the
   transport built — the contract, not the field.

2. **Live read-only smoke (Step 2).** Opt-in via ``SANCTUM_LIVE_HUB=1`` so the
   default gate (CI, clean checkout, overnight build) NEVER touches the live Bell
   hub. Read-only: ``get("Device/DeviceInfo/SoftwareVersion")`` → asserts a
   non-empty version. No ``set``. Skipped unless the env var is set.
"""

from __future__ import annotations

import json
import os

import pytest

# This module drives the REAL ``sagemcom_api`` encoding path on purpose (the
# whole premise of the boundary test). It is an OPTIONAL transport dependency
# declared in the ``hub`` extra (pulled in by ``dev``); guard the whole module
# so a contributor who installed without it gets a clean SKIP rather than a
# collection ERROR. The gate (``pip install -e ".[dev]"``) has it, so it runs.
pytest.importorskip("sagemcom_api")

from sanctum_cli.devices.base import Creds

BRIDGE_PATH = "Device/Services/BellNetworkCfg/SetBridgeMode"

# A deliberately hostile path/value: a literal '%', a space, and a non-ASCII
# char (CLAUDE.md: never test the boundary with "Deals/Calder"). If the provider
# double-encoded, the '%' would round-trip wrong.
HOSTILE_PATH = "Device/Services/BellNetworkCfg/Leaf%41 café"
HOSTILE_VALUE = "on %20 café 50%"


class _RealEncodingClient:
    """Drive the REAL ``sagemcom_api`` encoding with the network layer mocked.

    Wraps an actual :class:`sagemcom_api.client.SagemcomClient` and replaces only
    its private (name-mangled) ``__post`` — the genuinely expensive layer (a real
    socket) — with a recorder. Everything from ``set_value_by_xpath`` /
    ``get_value_by_xpath`` down to ``__api_request_async`` runs for real, so the
    SAH ``actions`` (incl. the ``urllib.parse.quote`` of the xpath) are exactly
    what would hit the wire. ``sent_actions`` exposes the last request's actions.

    The real client's ``aiohttp`` session/connector binds to the running event
    loop *at construction time*, so the inner client is built lazily inside
    :meth:`login` — which the provider drives on its own persistent loop — rather
    than in ``__init__`` (which the provider calls outside any running loop).
    """

    def __init__(self, reboot_reply: dict | None = None) -> None:
        self._client: object | None = None
        self.sent_actions: list[dict] = []
        # The SAH reply the (real) transport returns for the reboot action. The
        # DEFAULT is the shape the production code documents a clean reboot as
        # ('XMO_REQUEST_NO_ERR') — a test of the fail-closed path passes a rejected
        # envelope instead. None means "use the clean default".
        self._reboot_reply: dict = (
            reboot_reply
            if reboot_reply is not None
            else {"reply": {"error": {"description": "XMO_REQUEST_NO_ERR"}}}
        )

    def _build_inner(self) -> object:
        from sagemcom_api.client import SagemcomClient
        from sagemcom_api.enums import EncryptionMethod

        client = SagemcomClient(
            "192.168.2.1", "admin", "pw", EncryptionMethod.SHA512, ssl=False
        )
        # A valid (int-able) session id so the payload builder does not choke.
        client._session_id = 0  # test seam into the real client

        async def fake_post(url: str, data: dict) -> dict:
            actions = json.loads(data["req"])["request"]["actions"]
            self.sent_actions = actions
            action = actions[0]
            if action["method"] == "setValue":
                # Mirror the real reply shape for a successful setValue.
                return {"reply": {"error": {"description": "XMO_NO_ERR"}}}
            if action["method"] == "reboot":
                # The reboot action's reply rides back through the REAL
                # ``__api_request_async`` VERBATIM (the transport returns the
                # ``__post`` result unchanged) — so the provider's ``reboot()``
                # inspects exactly this envelope via ``_reply_error``. The reboot
                # action carries NO result callbacks, so the library's lossy
                # ``__get_response_value`` would extract ``None`` here regardless of
                # success/failure — which is precisely why the provider must take
                # the raw path and read this ``error.description`` itself.
                return self._reboot_reply
            if action["method"] in ("addChild", "deleteChild", "applyChanges"):
                # The table/transaction verbs ride the SAME raw seam as reboot and
                # carry the SAME clean-write envelope a successful setValue does, so
                # the provider's ``_reply_error`` inspects exactly this shape.
                return {"reply": {"error": {"description": "XMO_NO_ERR"}}}
            # getValue: return a value-shaped reply the client can unwrap.
            return {
                "reply": {
                    "error": {"description": "XMO_NO_ERR"},
                    "actions": [
                        {
                            "error": {"description": "XMO_NO_ERR"},
                            "callbacks": [{"parameters": {"value": "off"}}],
                        }
                    ],
                }
            }

        # Replace ONLY the network boundary; the encoding layers above run real.
        object.__setattr__(client, "_SagemcomClient__post", fake_post)
        return client

    # The provider only ever calls these coroutines (all on its persistent loop).
    async def login(self) -> None:
        # Built here so aiohttp binds to the provider's running loop.
        self._client = self._build_inner()

    async def logout(self) -> None:
        return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()  # type: ignore[attr-defined]

    async def get_value_by_xpath(self, xpath: str, options: dict | None = None) -> str | None:
        assert self._client is not None
        return await self._client.get_value_by_xpath(xpath, options)  # type: ignore[attr-defined]

    async def set_value_by_xpath(
        self, xpath: str, value: str, options: dict | None = None
    ) -> dict:
        assert self._client is not None
        return await self._client.set_value_by_xpath(xpath, value, options)  # type: ignore[attr-defined]

    # The raw seam the provider's ``reboot()`` reaches via ``getattr`` (its
    # name-mangled form). Delegating to the INNER real client means the reboot
    # action is built + serialized by the genuine ``__api_request_async`` and the
    # reply rides back through ``_reply_error`` — so reboot is proven through the
    # real transport, not a synthetic reply shape the production code never sees.
    async def _SagemcomClient__api_request_async(  # noqa: N802 - must mirror the lib's name-mangled raw seam
        self, actions: list, priority: bool = False
    ) -> dict:
        assert self._client is not None
        raw = self._client._SagemcomClient__api_request_async  # type: ignore[attr-defined]
        return await raw(actions, priority)


@pytest.fixture
def real_encoding(monkeypatch: pytest.MonkeyPatch) -> _RealEncodingClient:
    """A connected provider whose transport runs the real SAH encoding."""
    fake = _RealEncodingClient()
    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    return fake


def test_hostile_path_value_encoded_correctly_at_sah_boundary(
    real_encoding: _RealEncodingClient,
) -> None:
    """A set with a hostile path/value is encoded once, correctly, for the SAH wire.

    Asserts against the REAL request the transport built — not a field the fake
    recorded before encoding. The xpath must be URL-quoted exactly once (literal
    '%' → '%25', space → '%20', 'é' → '%C3%A9', '/' preserved) and the value must
    ride verbatim in the JSON ``parameters.value`` (the library does not URL-quote
    the value — it is JSON-serialized into the body). Double-encoding by the
    provider would corrupt the '%' to '%2525' and is the bug this guards.
    """
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    try:
        p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
        p.set(HOSTILE_PATH, HOSTILE_VALUE)
    finally:
        p.disconnect()

    action = real_encoding.sent_actions[0]
    assert action["method"] == "setValue"
    # xpath: encoded exactly once. The literal '%' MUST be '%25' (not preserved,
    # not double-quoted to '%2525'); space '%20'; 'é' '%C3%A9'; '/' kept literal.
    assert action["xpath"] == "Device/Services/BellNetworkCfg/Leaf%2541%20caf%C3%A9"
    assert "%2525" not in action["xpath"]  # would mean double-encoding
    # value: verbatim in the JSON body (no URL-quoting of the value).
    assert action["parameters"]["value"] == HOSTILE_VALUE


def test_hostile_path_passed_verbatim_to_library_get(
    real_encoding: _RealEncodingClient,
) -> None:
    """get() must hand the raw path to the library so it is quoted exactly once.

    If the provider pre-encoded, the getValue xpath would carry '%2525' for the
    literal '%'. We assert the single-quote correct form.
    """
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    try:
        p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
        p.get(HOSTILE_PATH)
    finally:
        p.disconnect()

    # The last request the transport built was the getValue (connect's brand-refine
    # get is for a different, known path; the hostile get is last).
    action = real_encoding.sent_actions[0]
    assert action["method"] == "getValue"
    assert action["xpath"] == "Device/Services/BellNetworkCfg/Leaf%2541%20caf%C3%A9"
    assert "%2525" not in action["xpath"]


# ─── reboot through the REAL transport (Task a) ──────────────────────────────


def test_reboot_issues_real_sah_action_through_api_request_async(
    real_encoding: _RealEncodingClient,
) -> None:
    """reboot() drives the SAH reboot action through the REAL ``__api_request_async``.

    The earlier ``test_reboot.py`` proves the provider routes via the raw seam, but
    against a FAKE that returns a synthetic reply shape it authored. This drives the
    *genuine* ``sagemcom_api`` transport (only ``__post`` mocked): the reboot action
    is built + JSON-serialized by the real ``__api_request_async``, so the bytes that
    would hit the wire — captured in ``sent_actions`` — are exactly the library's,
    not the production code's hope. The action MUST be the SAH reboot shape
    (``method == "reboot"``, ``xpath == "Device"``, ``parameters.source == "GUI"``),
    and a clean reply yields a successful OpResult.
    """
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    p = SagemcomHubProvider()
    try:
        p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
        result = p.reboot()
    finally:
        p.disconnect()

    action = real_encoding.sent_actions[0]
    assert action["method"] == "reboot"
    assert action["xpath"] == "Device"
    assert action["parameters"]["source"] == "GUI"
    assert result.ok is True
    assert result.detail == "reboot issued"


def test_reboot_fails_closed_on_rejected_reply_through_real_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected reboot reply the REAL transport RETURNS (not raises) fails closed.

    The transport returns the ``__post`` result verbatim, including an
    ``error.description`` it does not model — so "the call did not raise" is NOT
    proof the reboot landed. With the real transport returning a rejected envelope,
    ``reboot()`` MUST raise :class:`DeviceError` rather than report a green reboot.
    """
    from sanctum_cli.devices.base import DeviceError
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    fake = _RealEncodingClient(
        reboot_reply={"reply": {"error": {"description": "XMO_ACCESS_RESTRICTION_ERR"}}}
    )
    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")

    p = SagemcomHubProvider()
    try:
        p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
        with pytest.raises(DeviceError):
            p.reboot()
        # Proof it really issued the reboot action through the real transport.
        action = fake.sent_actions[0]
        assert action["method"] == "reboot"
    finally:
        p.disconnect()


@pytest.mark.parametrize(
    "token",
    ["XMO_ACTION_CALLBACK_ERR", "XMO_REBOOTING_ERR"],
)
def test_reboot_initiated_token_is_success_through_real_transport(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """FIX-1, through the REAL transport: a reboot-initiated token reply is SUCCESS.

    A SAH reboot kills its own connection, so the hub returns
    XMO_ACTION_CALLBACK_ERR / XMO_REBOOTING_ERR (or drops the connection) rather
    than a clean XMO_NO_ERR — that is the NORMAL signal the reboot started. The
    reply rides back through the genuine ``__api_request_async`` verbatim, so this
    drives the real transport (only ``__post`` mocked) and asserts ``reboot()``
    returns ``ok=True`` for the token the device actually emits — the contract, not
    the production code's old XMO_NO_ERR-only assumption (which raised here = the
    06-26 root cause).
    """
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    fake = _RealEncodingClient(reboot_reply={"reply": {"error": {"description": token}}})
    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")

    p = SagemcomHubProvider()
    try:
        p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
        result = p.reboot()
    finally:
        p.disconnect()

    # The reboot action really crossed the real transport …
    action = fake.sent_actions[0]
    assert action["method"] == "reboot"
    # … and the reboot-initiated token was read as SUCCESS, not a failed stage.
    assert result.ok is True


# ─── table-row ops through the REAL transport (hostile xpath) ────────────────


def test_add_row_hostile_xpath_quoted_once_at_sah_boundary(
    real_encoding: _RealEncodingClient,
) -> None:
    """add_row() must own the xpath encoding: a hostile table xpath is URL-quoted
    exactly once for the SAH wire, and the params ride verbatim in the JSON body.

    Unlike ``set``/``get`` (which pass the path verbatim to the library setter and
    let ``sagemcom_api`` quote it), the table verbs issue through the RAW seam,
    which does NOT quote — so the provider owns the single layer of encoding
    itself (CLAUDE.md: own the escaping at the boundary). This drives the genuine
    transport (only ``__post`` mocked) and asserts the bytes that would hit the
    wire: the literal '%' → '%25' (never preserved, never double-quoted to
    '%2525'), space → '%20', 'é' → '%C3%A9', '/' kept literal; the params dict
    rides verbatim (no URL-quoting of values — they are JSON-serialized).
    """
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    params = {"ExternalPort": "8443", "Comment": "café 50%"}
    p = SagemcomHubProvider()
    try:
        p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
        p.add_row(HOSTILE_PATH, params)
    finally:
        p.disconnect()

    action = real_encoding.sent_actions[0]
    assert action["method"] == "addChild"
    assert action["xpath"] == "Device/Services/BellNetworkCfg/Leaf%2541%20caf%C3%A9"
    assert "%2525" not in action["xpath"]  # would mean double-encoding
    # Params (incl. a value carrying a literal '%') ride verbatim in the JSON body.
    assert action["parameters"] == params


def test_delete_row_index_preserved_through_real_transport(
    real_encoding: _RealEncodingClient,
) -> None:
    """delete_row() issues a real ``deleteChild`` carrying the instance index.

    Driven through the genuine ``__api_request_async`` serialization so the
    ``deleteChild`` action — verb, table xpath, and ``{"index": n}`` parameters —
    is exactly what would hit the wire, not a field a convenient fake recorded.
    """
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    table = "Device/NAT/PortMapping"
    p = SagemcomHubProvider()
    try:
        p.connect(Creds(host="192.168.2.1", username="admin", secret=None, key_path=None))
        result = p.delete_row(table, 4)
    finally:
        p.disconnect()

    action = real_encoding.sent_actions[0]
    assert action["method"] == "deleteChild"
    assert action["xpath"] == table
    assert action["parameters"] == {"index": 4}
    assert result.ok is True


# ─── version-guard the reboot fail-closed seam (Task b) ──────────────────────


def test_installed_client_exposes_raw_api_request_seam() -> None:
    """The installed ``sagemcom_api`` MUST expose the name-mangled raw seam.

    ``SagemcomHubProvider.reboot`` is fail-closed ONLY because it issues the reboot
    through the client's raw ``_SagemcomClient__api_request_async`` — which returns
    the full ``{"reply": {"error": ...}}`` envelope the fail-closed check reads. If a
    future ``sagemcom_api`` release drops/renames that mangled method, the
    production code (:func:`sanctum_cli.devices.sagemcom._reboot_raw`) silently falls
    back to the convenience ``reboot()`` wrapper — which extracts a leaf value
    (``None`` for the reboot action, with no callbacks) and CANNOT tell a clean
    reboot from a rejected one, defeating fail-closed.

    This is the version guard: it FAILS LOUDLY the moment an upgrade removes the
    seam, so the fallback degradation is caught at test time (a known-good build)
    rather than surfacing as a reboot that reports green on a rejected cutover. It
    also asserts the public ``reboot`` coroutine still exists (the documented
    fallback path) and that the raw seam is a coroutine function with the
    ``(actions, priority)`` arity the production code calls it with.
    """
    import inspect

    from sagemcom_api.client import SagemcomClient

    raw = getattr(SagemcomClient, "_SagemcomClient__api_request_async", None)
    assert raw is not None, (
        "installed sagemcom_api dropped/renamed _SagemcomClient__api_request_async — "
        "the reboot fail-closed path falls back to the lossy reboot() wrapper; "
        "update sanctum_cli.devices.sagemcom._reboot_raw to the new seam."
    )
    assert inspect.iscoroutinefunction(raw), "the raw seam must be a coroutine"
    # The production code calls it as ``raw([action], False)`` — assert that arity
    # (self + actions + priority) is still available so the call site stays valid.
    params = list(inspect.signature(raw).parameters)
    assert params[:2] == ["self", "actions"], (
        f"raw seam signature changed: {params!r} — _reboot_raw calls it as "
        "raw([action], priority)"
    )
    # The documented defensive fallback still exists too.
    assert hasattr(SagemcomClient, "reboot")


# ─── Live read-only smoke (Step 2) — opt-in, default-skipped ─────────────

LIVE_HUB = os.environ.get("SANCTUM_LIVE_HUB") == "1"


@pytest.mark.skipif(
    not LIVE_HUB,
    reason="live hub smoke is opt-in: set SANCTUM_LIVE_HUB=1 to run (read-only, no mutation)",
)
def test_live_hub_read_only_software_version() -> None:
    """Read-only smoke against the REAL Bell hub — opt-in, never mutates.

    Connects with the Keychain password and reads the firmware version leaf,
    asserting a non-empty string. NO ``set`` is performed. Skipped unless
    ``SANCTUM_LIVE_HUB=1`` so the default gate (CI/overnight) never opens a
    socket to the live gateway.
    """
    from sanctum_cli.devices.sagemcom import SagemcomHubProvider

    host = os.environ.get("SANCTUM_LIVE_HUB_HOST", "192.168.2.1")
    p = SagemcomHubProvider()
    try:
        p.connect(Creds(host=host, username="admin", secret=None, key_path=None))
        version = p.get("Device/DeviceInfo/SoftwareVersion")
    finally:
        p.disconnect()
    assert version, "live hub returned an empty SoftwareVersion"
