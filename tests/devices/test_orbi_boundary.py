"""pynetgear/SOAP-boundary encoding contract + registry listing (Task 3).

Two concerns live here, both about the *boundary* between the Orbi provider and
the real ``pynetgear`` SOAP transport — the seam where a caller-supplied value
crosses into the SOAP request body that hits the wire.

1. **Hostile-input boundary.** CLAUDE.md "Own the escaping at the boundary; test
   the hostile input, not the happy path": a value carrying a literal ``%``, a
   space, AND a non-ASCII char must reach the SOAP request *correctly* — and for
   the pynetgear transport that means **verbatim**. ``pynetgear`` builds its SOAP
   body by raw string interpolation of each param into XML
   (``"<" + k + ">" + value + "</" + k + ">"`` in ``Netgear._make_request``) — it
   does NOT URL-encode the value. So the boundary contract is: the provider hands
   the value to pynetgear *as-is*, and the single, library-owned layer (raw XML
   interpolation) is what hits the wire. The footgun this guards is a provider
   that "helpfully" pre-encodes (``quote``/escape) the value: a literal ``%``
   would become ``%25`` and silently write the wrong value, every normal value
   passing and the call misfiring only on the one pathological row.

   This module drives the **real** ``pynetgear`` SOAP-construction/encoding path
   and mocks ONLY the genuinely expensive layer (the socket, via
   ``requests.post``), then asserts on the real SOAP body the transport built —
   the bytes that would hit the wire, not a field a fake recorded before encoding
   (a test cannot catch a bug it short-circuits). The recorded pynetgear call is
   the contract, not the field.

   SAFETY: ``requests.post`` is mocked — no request ever leaves the process;
   nothing touches a live Orbi (192.168.1.1 / the SOAP ``:5000`` endpoint). No
   live mutation is fired; ``need_auth=False`` so no login socket opens either.

2. **Registry lists "orbi".** A direct assertion that the provider self-registered
   on import so ``registry.resolve("orbi", ...)`` can find it — the spec success
   criterion that adding a brand is one new module + one registry line.
"""

from __future__ import annotations

import re

import pytest

# This module drives the REAL ``pynetgear`` SOAP encoding path on purpose (the
# whole premise of the boundary test). It is a declared transport dependency; the
# gate (``pip install -e ".[dev]"``) has it. Guard the import so a contributor who
# somehow installed without it gets a clean SKIP rather than a collection ERROR.
pynetgear = pytest.importorskip("pynetgear")

import pynetgear.const as c  # noqa: E402 - after importorskip on purpose

# A deliberately hostile param value: a literal '%' (data, not a percent-escape we
# want preserved), a space, AND a non-ASCII char (CLAUDE.md: never test the
# boundary with "Deals/Calder"). If the provider pre-encoded, the '%' would
# round-trip wrong as '%25' and silently write the wrong value.
HOSTILE_VALUE = "on %20 café 50%"

# A hostile host: same '%' / space / non-ASCII triple, to prove the host the
# caller supplies rides into the SOAP URL verbatim (pynetgear does not encode it).
HOSTILE_HOST = "café-router%41 box"


def _capture_post(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Mock ONLY ``requests.post`` (the socket); record each (url, body).

    Everything from the pynetgear setter/``_make_request`` down to the SOAP body
    string runs for real, so the recorded ``body`` is exactly what would hit the
    wire (incl. pynetgear's raw XML interpolation of the params). No socket opens.
    """
    seen: list[tuple[str, str]] = []

    class _FakeResp:
        status_code = 200
        text = "<ResponseCode>000</ResponseCode>"

    def fake_post(url, headers=None, data=None, timeout=None, verify=None):  # type: ignore[no-untyped-def]
        seen.append((url, data))
        return _FakeResp()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    return seen


def test_hostile_value_rides_verbatim_into_soap_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile value reaches the SOAP body verbatim — pynetgear does not encode it.

    Asserts against the REAL SOAP request the transport built. pynetgear
    interpolates the param value raw into XML, so the contract is verbatim
    pass-through: the literal '%' stays '%' (NOT '%25'), the space stays a space,
    'é' stays 'é'. If a provider/transport double-encoded, '%' would become '%25'
    (and a double-encode '%2525') and the wrong value would be written — the bug
    this boundary test guards. We drive ``_make_request`` directly with the hostile
    value so the value-crossing contract is proven against the recorded call.
    """
    captured = _capture_post(monkeypatch)

    netgear = pynetgear.Netgear(password="pw", host=HOSTILE_HOST, user="admin")
    netgear.cookie = "c"  # test seam: skip the login socket
    netgear._make_request(
        c.SERVICE_WLAN_CONFIGURATION,
        "TestSetMethod",
        params={"NewVal": HOSTILE_VALUE},
        need_auth=False,
    )

    assert captured, "no request reached the transport"
    url, body = captured[-1]
    # The value rode into the SOAP body verbatim (raw XML interpolation).
    match = re.search(r"<NewVal>(.*?)</NewVal>", body, re.DOTALL)
    assert match is not None, "param not found in SOAP body"
    assert match.group(1) == HOSTILE_VALUE
    # Encoded ZERO times by the transport — the literal '%' must survive as data.
    assert "%25" not in body  # would mean something URL-encoded the value
    assert "café" in body  # non-ASCII survived unmangled
    # And the hostile host rode into the SOAP URL verbatim (not pre-encoded).
    assert HOSTILE_HOST in url
    assert "%2541" not in url  # would mean the host was double-encoded


def test_provider_guest_set_lands_in_soap_body_through_real_pynetgear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END-TO-END: provider.set(guest) → real pynetgear → recorded SOAP body.

    Drives the provider's own ``set`` of a guest-wifi leaf through the REAL
    pynetgear ``set_5g_guest_access_enabled`` with only the socket mocked, and
    asserts the value the provider intended ('on' → engaged) lands in the SOAP
    body pynetgear built. This proves the value crosses every real layer — the
    provider's path→setter mapping, pynetgear's bool→'1' normalization, the raw
    XML interpolation — and arrives on the wire as ``<NewGuestAccessEnabled>1``.
    No field recorded before encoding; the recorded pynetgear call is the contract.
    """
    from sanctum_cli.devices.base import Creds
    from sanctum_cli.devices.orbi import OrbiProvider

    captured = _capture_post(monkeypatch)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")

    # Real pynetgear client (not the FakeNetgear) so the SOAP encoding is real.
    def real_client(creds: Creds) -> object:
        client = pynetgear.Netgear(
            password=creds.secret or "", host=creds.host, user=creds.username
        )
        client.cookie = "c"  # test seam: skip the login socket
        return client

    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", real_client)

    provider = OrbiProvider()
    try:
        provider.connect(Creds(host="192.168.1.1", username="admin", secret=None, key_path=None))
        # Re-establish an authenticated-session cookie post-connect. ``connect``'s
        # best-effort ``login()`` cannot truly authenticate against the mocked
        # socket (the fake reply is not a real login response), and pynetgear's
        # ``login`` clears the cookie — so without this the per-call re-login
        # inside ``_try_request`` keeps failing and the set never reaches its real
        # SOAP call. The provider retains the client; setting its cookie is the
        # test seam for "this session is authed", letting the REAL set SOAP body
        # be built and recorded.
        provider._client.cookie = "c"  # type: ignore[union-attr]
        captured.clear()  # drop the connect-time login POST; assert only on the set
        provider.set("guest_wifi/5g", "on")
    finally:
        provider.disconnect()

    # The set must have produced a real Set5GGuestAccessEnabled SOAP body.
    bodies = [body for _url, body in captured if "Set5GGuestAccessEnabled" in body]
    assert bodies, "provider.set never reached a real SOAP Set5GGuestAccessEnabled call"
    match = re.search(r"<NewGuestAccessEnabled>(.*?)</NewGuestAccessEnabled>", bodies[-1])
    assert match is not None
    assert match.group(1) == "1"  # 'on' → engaged → pynetgear bool→'1'


def test_provider_does_not_pre_encode_a_hostile_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile, unknown path is refused (ok=False), NOT silently sent to the wire.

    The provider addresses a fixed, brand-owned path vocabulary; a caller-supplied
    path carrying '%'/space/non-ASCII is not a writable leaf, so ``set`` returns
    ``ok=False`` (the rails treat it as a failed apply) — it must never be
    interpolated, encoded-or-not, into a SOAP request. Proven against the recorded
    transport: zero requests reach the wire for the hostile path.
    """
    from sanctum_cli.devices.base import Creds
    from sanctum_cli.devices.orbi import OrbiProvider

    captured = _capture_post(monkeypatch)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")

    def real_client(creds: Creds) -> object:
        client = pynetgear.Netgear(
            password=creds.secret or "", host=creds.host, user=creds.username
        )
        client.cookie = "c"
        return client

    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", real_client)

    hostile_path = "guest_wifi/abc%41 café/5g"
    provider = OrbiProvider()
    try:
        provider.connect(Creds(host="192.168.1.1", username="admin", secret=None, key_path=None))
        provider._client.cookie = "c"  # type: ignore[union-attr] # authed-session seam
        captured.clear()  # drop the connect-time login POST; assert only on the set
        result = provider.set(hostile_path, HOSTILE_VALUE)
    finally:
        provider.disconnect()

    assert result.ok is False  # not a writable leaf → failed apply, not a write
    # And the hostile path/value never reached the wire — neither raw nor encoded.
    assert not captured  # the set issued no SOAP request at all for the hostile path
    bodies = "".join(body for _url, body in captured)
    assert "café" not in bodies
    assert "abc" not in bodies


# ── registry now lists "orbi" ─────────────────────────────────────────


def test_registry_lists_orbi() -> None:
    """The provider self-registered on import → resolve('orbi', ...) finds it."""
    from sanctum_cli.devices import orbi, registry

    assert "orbi" in registry._REGISTRY
    assert orbi.OrbiProvider in registry._REGISTRY["orbi"]
    # And the class advertises the brand/kind the registry buckets/pins it under.
    assert orbi.OrbiProvider.brand == "orbi"
    assert orbi.OrbiProvider.kind == "orbi"
