"""Bridge/SSH-boundary encoding contract + registry listing (Task 5).

Two concerns live here, both about the *boundary* between the Firewalla provider
and the real bridge HTTP transport — the seam where a caller-supplied target /
policy id crosses into the ``httpx`` request URL.

1. **Hostile-input boundary.** CLAUDE.md "Own the escaping at the boundary; test
   the hostile input, not the happy path": a policy/target id carrying a literal
   ``%``, a space, AND a non-ASCII char must be percent-encoded *exactly once*
   before it reaches ``httpx``. The footgun is specific and named in CLAUDE.md:
   ``httpx`` *preserves* an existing ``%``-sequence, so a path whose id literally
   contains ``%41`` would ride to the wire as ``%41`` (decoding server-side to the
   letter ``A``) and silently address the WRONG policy — every normal id passes
   and the call misfires on the one pathological row. The fix is to own the
   encoding at the boundary (``quote(path, safe='/')``) so the literal ``%``
   becomes ``%2541`` — encoded once, not preserved, not double-encoded to
   ``%2525``.

   This module drives the **real** ``httpx`` URL-construction/encoding path and
   mocks ONLY the genuinely expensive layer (the socket, via
   ``httpx.MockTransport``), then asserts on the real ``request.url.raw_path`` the
   client built — the contract that hits the wire, not a field recorded before
   encoding (a test cannot catch a bug it short-circuits). Both the GET read seam
   (``_fetch_bridge_json``) and the POST mutate seam (``_post_bridge_json``) are
   covered, since both interpolate a caller-supplied path into the URL.

   SAFETY: ``MockTransport`` intercepts at the socket layer — no request ever
   leaves the process; nothing touches the live Firewalla (10.0.0.1 /
   firewalla.local). The POST seam is exercised only against the mock; no live
   mutation is fired.

2. **Registry lists "firewalla".** A direct assertion that the provider
   self-registered on import so ``registry.resolve("firewalla", ...)`` can find
   it — the spec success criterion that adding a brand is one new module + one
   registry line.
"""

from __future__ import annotations

import httpx
import pytest

from sanctum_cli.devices import firewalla as fw

# A deliberately hostile policy id: a literal '%41' (NOT a percent-escape we want
# preserved — it is data), a space, and a non-ASCII char (CLAUDE.md: never test
# the boundary with "Deals/Calder"). The id is embedded in a bridge path so the
# slashes around it must stay literal while the id's bytes are encoded.
HOSTILE_PATH = "/policy/abc%41 café/pause"

# What MUST hit the wire: '/' preserved, literal '%' → '%25' (so '%41' → '%2541',
# the id's own bytes — NOT the letter 'A'), space → '%20', 'é' → '%C3%A9'.
EXPECTED_RAW_PATH = "/policy/abc%2541%20caf%C3%A9/pause"


@pytest.fixture
def capture_transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Intercept the real httpx request at the socket layer; record each Request.

    Installs an ``httpx.MockTransport`` as the default the provider's transport
    seams build their client on, so the REAL httpx URL-construction/encoding runs
    (the whole premise of the boundary test) but no socket opens. Returns the list
    of captured ``httpx.Request`` objects so a test can assert on
    ``request.url.raw_path`` — the exact bytes that would hit the wire.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(fw, "_bridge_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(fw, "_read_bridge_token", lambda: "tok")
    return seen


def test_get_hostile_path_encoded_exactly_once_at_bridge(
    capture_transport: list[httpx.Request],
) -> None:
    """GET seam: a hostile policy id is percent-encoded exactly once for the wire.

    Asserts against the REAL request the client built. The literal '%' MUST be
    '%25' (the id's '%41' → '%2541', addressing the id whose name contains '%41',
    NOT the policy 'A'); space '%20'; 'é' '%C3%A9'; the path '/' separators kept
    literal. If the provider leaned on httpx's incidental behaviour, '%41' would
    be PRESERVED (silently addressing the wrong policy). If it double-encoded,
    '%2525' would appear. This guards both failure modes.
    """
    fw._fetch_bridge_json(HOSTILE_PATH)

    assert capture_transport, "no request reached the transport"
    raw = capture_transport[-1].url.raw_path.decode("ascii")
    assert raw == EXPECTED_RAW_PATH
    assert "%2525" not in raw  # would mean the provider double-encoded
    # The literal '%' must be encoded, not preserved as a live escape:
    assert "abc%2541" in raw
    assert "/abcA/" not in raw  # httpx-preserved '%41' would decode to 'A'


def test_post_hostile_path_encoded_exactly_once_at_bridge(
    capture_transport: list[httpx.Request],
) -> None:
    """POST (mutate) seam: same exactly-once encoding for a hostile policy id.

    The mutate path interpolates the same caller-supplied id into the URL, so the
    boundary contract is identical — proven here against the real httpx request so
    a pause/set that targets a '%'-bearing policy id addresses the right policy.
    """
    fw._post_bridge_json(HOSTILE_PATH, {"value": "true"})

    assert capture_transport, "no request reached the transport"
    raw = capture_transport[-1].url.raw_path.decode("ascii")
    assert raw == EXPECTED_RAW_PATH
    assert "%2525" not in raw
    assert "/abcA/" not in raw


def test_already_safe_path_unchanged_through_boundary(
    capture_transport: list[httpx.Request],
) -> None:
    """A normal, already-safe path rides verbatim — encoding is a no-op for it.

    Guards the other direction: owning the encoding must not corrupt the common
    case. '/info' has no reserved bytes, so the raw path that hits the wire is
    exactly '/info'.
    """
    fw._fetch_bridge_json("/info")

    assert capture_transport, "no request reached the transport"
    assert capture_transport[-1].url.raw_path.decode("ascii") == "/info"


def test_host_mac_colon_stays_literal_through_boundary(
    capture_transport: list[httpx.Request],
) -> None:
    """REGRESSION: the screen-time ``/host/<mac>`` read path keeps its colons literal.

    A MAC like ``AA:BB:CC:00:00:01`` is a legal, literal URL path string — every
    ``:`` is an RFC-3986 sub-delim-adjacent pchar. The screen-time compat gate
    (``screen_time.compat_command`` → ``_fetch_bridge_json(f"/host/{mac}")``)
    decides whether a child's device is enforced, so the wire path MUST be
    byte-identical to the pre-refactor ``/host/AA:BB:CC:00:00:01``. An earlier cut
    used ``quote(path, safe="/")``, which encoded each ``:`` to ``%3A`` and rested
    on the unverifiable cross-layer assumption that the bridge route-matches
    ``%3A`` the same as ``:`` — an unrequested behavior change to a shipped live
    read path. Driven through the REAL httpx encoding seam (the safety-net hole
    was that the characterization test monkeypatched ``st._fetch_bridge_json``
    wholesale, bypassing exactly this seam).
    """
    mac = "AA:BB:CC:00:00:01"
    fw._fetch_bridge_json(f"/host/{mac}")

    assert capture_transport, "no request reached the transport"
    raw = capture_transport[-1].url.raw_path.decode("ascii")
    assert raw == "/host/AA:BB:CC:00:00:01"  # byte-identical to the old wire path
    assert "%3A" not in raw  # the colon must NOT be percent-encoded


# ── net.py + provider seam: encoding owned by exactly ONE layer ───────


def test_pause_path_plus_provider_seam_encode_hostile_target_exactly_once(
    capture_transport: list[httpx.Request],
) -> None:
    """END-TO-END: ``_fw_pause_path`` + the provider POST seam encode a hostile
    target exactly once — no double-encoding to ``%2525``.

    The bug class (CLAUDE.md "a test cannot catch a bug it shares"): both
    ``net._fw_pause_path`` and the provider seam used to percent-encode, both unit
    tests passed in isolation, but a literal-``%`` policy id misfired end-to-end as
    ``%2525``. This test drives the TWO layers TOGETHER — the CLI builds the path,
    the provider seam puts it on the real httpx wire — with a target carrying a
    literal ``%41``, a space, AND a non-ASCII char, and asserts the wire bytes are
    encoded ONCE. Ownership now lives solely in the provider seam; ``_fw_pause_path``
    interpolates the target verbatim.
    """
    from sanctum_cli.commands import net

    target = "abc%41 café"  # literal '%41' (data, not an escape), space, non-ASCII
    path = net._fw_pause_path(target)
    # The CLI path is RAW — the per-call quote was removed (single-ownership).
    assert path == "/policies/abc%41 café/pause"

    # Cross the provider boundary exactly as the apply path does (provider.set →
    # _post_bridge_json). The mock transport records the real wire bytes.
    fw._post_bridge_json(path, {"value": "paused"})

    assert capture_transport, "no request reached the transport"
    raw = capture_transport[-1].url.raw_path.decode("ascii")
    # Encoded EXACTLY once: '%41' → '%2541' (the id's own bytes), space → '%20',
    # 'é' → '%C3%A9', the '/policies/.../pause' separators kept literal.
    assert raw == "/policies/abc%2541%20caf%C3%A9/pause"
    assert "%2525" not in raw  # the double-encoding the design must prevent
    assert "abc%2541" in raw  # the literal '%41' survived as data, encoded once


# ── registry now lists "firewalla" ────────────────────────────────────


def test_registry_lists_firewalla() -> None:
    """The provider self-registered on import → resolve('firewalla', ...) finds it."""
    from sanctum_cli.devices import registry

    assert "firewalla" in registry._REGISTRY
    assert fw.FirewallaProvider in registry._REGISTRY["firewalla"]
    # And the class advertises the brand the registry buckets/pins it under.
    assert fw.FirewallaProvider.brand == "firewalla"
    assert fw.FirewallaProvider.kind == "firewalla"
