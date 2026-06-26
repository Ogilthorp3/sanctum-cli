"""Firewalla named ops + generic POST/DELETE — REAL httpx, route-correct bodies.

The prior ``set()`` POSTed a hardcoded ``{"value": value}`` body that matches
ALMOST NO Firewalla bridge route — every bridge mutate is a *structured* POST
(``/host/:mac/policy``, ``/host/:mac/rules``, ``/feature/:name/enable``, the
``/box/*`` ops) or a DELETE (``/policy/:pid``, ``/dns/:hostname``). These tests
pin the REAL contract for each op, derived from the deployed bridge source
(``sanctum-screen-time/deployed/firewalla-bridge.js``) AND cross-checked against
the production ``screen_time.py`` caller (``_bridge_request`` sends
``json=(data or {})`` on POST, no body on DELETE) — an INDEPENDENT source from
this provider, so the test cannot share the producer's assumption
(CLAUDE.md "Contracts at the Boundary", sub-rule 2).

Every op is driven through the REAL ``_post_bridge_json`` / ``_delete_bridge_json``
seams via an ``httpx.MockTransport`` — the real httpx URL-construction, method,
and JSON body serialization run; only the socket is intercepted (sub-rule 3:
"don't mock cheap subprocess boundaries"). The capture asserts the exact HTTP
method, path, and body that would hit the wire — not a stub of the seam.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from sanctum_cli.devices import firewalla as fw
from sanctum_cli.devices.base import Capability, Creds, Snapshot
from sanctum_cli.devices.firewalla import FirewallaProvider


def _capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    reply: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Intercept the REAL httpx transport; capture method/path/raw_path/body.

    Returns a list the test asserts against. ``path`` is the decoded path
    (clean assertions); ``raw_path`` is the wire bytes (boundary-encoding proof).
    """
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.content
        captured.append(
            {
                "method": request.method,
                "path": request.url.path,
                "raw_path": request.url.raw_path.decode(),
                "body": json.loads(raw) if raw else None,
                "auth": request.headers.get("Authorization"),
            }
        )
        body = reply if reply is not None else {"success": True}
        return httpx.Response(status, request=request, content=json.dumps(body).encode())

    monkeypatch.setattr(fw, "_bridge_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(fw, "_read_bridge_token", lambda: "tok")
    monkeypatch.setattr(fw, "_resolve_ssh_key", lambda: None)
    return captured


def _provider(monkeypatch: pytest.MonkeyPatch) -> FirewallaProvider:
    """A connected provider whose /info probe is silenced (no extra captures)."""
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda *a, **k: None)
    p = FirewallaProvider()
    p.connect(Creds(host="firewalla.local", username="pi"))
    return p


MAC = "AA:BB:CC:DD:EE:FF"

# ── generic op: any POST route reachable with a route-correct body ────────


def test_op_posts_route_correct_body(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.op(f"/host/{MAC}/policy", {"family": True})
    assert res.ok is True
    assert cap[-1]["method"] == "POST"
    assert cap[-1]["path"] == f"/host/{MAC}/policy"
    # The route-correct body — NOT the prior {"value": ...} wrapper.
    assert cap[-1]["body"] == {"family": True}
    assert cap[-1]["auth"] == "Bearer tok"


def test_op_empty_body_posts_empty_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bodyless route (pause/box) posts {} on the wire — matches production."""
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    p.op("/box/reboot")
    assert cap[-1]["body"] == {}


def test_op_bridge_refusal_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, status=500, reply={"error": "boom"})
    p = _provider(monkeypatch)
    res = p.op("/box/reboot")
    assert res.ok is False


def test_op_before_connect_raises() -> None:
    from sanctum_cli.devices.base import DeviceError

    p = FirewallaProvider()
    with pytest.raises(DeviceError):
        p.op("/box/reboot")


# ── device_block: pause / unpause ─────────────────────────────────────────


def test_device_block_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.device_block(MAC, blocked=True)
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", f"/host/{MAC}/pause")
    assert cap[-1]["body"] == {}


def test_device_block_unpauses(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    p.device_block(MAC, blocked=False)
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", f"/host/{MAC}/unpause")


# ── device_policy: family / adblock / safeSearch / dhcp-reservation ───────


def test_device_policy_sends_only_provided_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    p.device_policy(MAC, family=True, adblock=False)
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", f"/host/{MAC}/policy")
    # Only the fields the caller set — bridge keys are camelCase where it matters.
    assert cap[-1]["body"] == {"family": True, "adblock": False}


def test_device_policy_safesearch_and_ipallocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """safeSearch + the dhcp-reservation (ipAllocation) ride the bridge's camelCase keys."""
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    reservation = {"type": "static", "ipv4": "192.168.1.50"}
    p.device_policy(MAC, safe_search=True, ip_allocation=reservation)
    assert cap[-1]["body"] == {"safeSearch": True, "ipAllocation": reservation}


def test_device_policy_no_fields_is_noop_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """No policy field set → no phantom POST; reports ok=False legibly."""
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.device_policy(MAC)
    assert res.ok is False
    assert cap == []  # nothing hit the wire


# ── device_rules: service block with expire ───────────────────────────────


def test_device_rules_block_with_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.device_rules(MAC, ["youtube", "tiktok"], action="block", expire=1900000000)
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", f"/host/{MAC}/rules")
    assert cap[-1]["body"] == {
        "services": ["youtube", "tiktok"],
        "action": "block",
        "expire": 1900000000,
    }


def test_device_rules_omits_expire_when_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    p.device_rules(MAC, ["discord"])
    assert cap[-1]["body"] == {"services": ["discord"], "action": "block"}


# ── feature_toggle ────────────────────────────────────────────────────────


def test_feature_toggle_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    p.feature_toggle("adblock", enabled=True)
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", "/feature/adblock/enable")
    assert cap[-1]["body"] == {}


def test_feature_toggle_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    p.feature_toggle("adblock", enabled=False)
    assert cap[-1]["path"] == "/feature/adblock/disable"


# ── local_dns: POST /dns + DELETE /dns/:hostname ──────────────────────────


def test_local_dns_set(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.local_dns_set("nas.lan", "192.168.1.10")
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", "/dns")
    assert cap[-1]["body"] == {"hostname": "nas.lan", "ip": "192.168.1.10"}


def test_local_dns_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.local_dns_delete("nas.lan")
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("DELETE", "/dns/nas.lan")
    assert cap[-1]["body"] is None  # DELETE carries no body, per the bridge contract


# ── DELETE /policy/:pid ───────────────────────────────────────────────────


def test_delete_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.delete_policy(42)
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("DELETE", "/policy/42")
    assert cap[-1]["body"] is None


def test_delete_policy_failure_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge returns 500 with {success:false} when a delete fails → ok=False."""
    _capture(monkeypatch, status=500, reply={"success": False, "pid": 42})
    p = _provider(monkeypatch)
    assert p.delete_policy(42).ok is False


# ── alarm-ack ─────────────────────────────────────────────────────────────


def test_alarm_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.alarm_ack("abc123")
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", "/alarm/abc123/ignore")
    assert cap[-1]["body"] == {}


# ── Wake-on-LAN ───────────────────────────────────────────────────────────


def test_wake_on_lan(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.wake_on_lan(MAC)
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", f"/host/{MAC}/wake")


# ── speedtest ─────────────────────────────────────────────────────────────


def test_speedtest(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.speedtest()
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", "/speedtest")


# ── box ops: reboot / shutdown / cancel / upgrade ─────────────────────────


@pytest.mark.parametrize(
    ("call", "path"),
    [
        ("box_reboot", "/box/reboot"),
        ("box_shutdown", "/box/shutdown"),
        ("box_shutdown_cancel", "/box/shutdown/cancel"),
        ("box_upgrade", "/box/upgrade"),
        ("reboot", "/box/reboot"),  # Protocol-aligned alias for intents._reboot
    ],
)
def test_box_ops(monkeypatch: pytest.MonkeyPatch, call: str, path: str) -> None:
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = getattr(p, call)()
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", path)
    assert cap[-1]["body"] == {}


def test_reboot_returns_opresult_for_intent_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """intents._reboot duck-types reboot() and requires an OpResult back."""
    from sanctum_cli.devices.base import OpResult

    _capture(monkeypatch)
    p = _provider(monkeypatch)
    assert isinstance(p.reboot(), OpResult)


# ── boundary: a hostile id is percent-encoded EXACTLY once on the wire ────


def test_named_op_owns_boundary_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname carrying a literal '%', a space, and non-ASCII rides encoded once.

    CLAUDE.md "Own the escaping at the boundary; test the hostile input": the id
    flows into the URL path. A literal '%41' must become '%2541' on the wire (the
    id's own bytes), NOT '%41' (which the bridge would decode to 'A', addressing
    the WRONG record). We assert the RAW wire path, the bytes the box actually
    route-matches — proving the provider, not httpx's incidental behavior, owns it.
    """
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    p.local_dns_delete("ev il%41.café")
    raw = cap[-1]["raw_path"]
    assert "%2541" in raw  # literal '%41' encoded once (NOT preserved as %41)
    assert "%20" in raw  # the space
    assert "%41" not in raw.replace("%2541", "")  # no un-escaped %41 survived


# ── capabilities: every advertised cap now has a real writable op ─────────


def test_capabilities_advertise_only_backed_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Honest-verify: each named cap is backed by a real, route-correct op above.

    WAN_MODE stays OUT (NAT/DMZ/WAN are GUI-only — no bridge route); REBOOT and the
    named device/feature/dns/box caps go IN because the methods proven above back
    them. No cap names an op that does not exist.
    """
    p = _provider(monkeypatch)
    caps = p.capabilities()
    expected = {
        Capability.READ,
        Capability.POLICY,
        Capability.SCREEN_TIME,
        Capability.DEVICE_BLOCK,
        Capability.DEVICE_POLICY,
        Capability.DEVICE_RULES,
        Capability.FEATURE_TOGGLE,
        Capability.LOCAL_DNS,
        Capability.ALARM_ACK,
        Capability.WAKE_ON_LAN,
        Capability.SPEEDTEST,
        Capability.REBOOT,
    }
    assert caps == expected
    assert Capability.WAN_MODE not in caps


# ── /raw escape hatch: POST /raw {type,item,value,target} ─────────────────


def test_raw_escape_hatch_posts_type_item_value_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generic escape hatch reaches POST /raw with the route's own body keys.

    Derived from the REAL bridge contract (firewalla-bridge.js: ``POST /raw`` reads
    ``const {type, item, value, target} = body`` and builds ``new FWMessage(type,
    {item, value}, target)``) — NOT a convenient fake. So the body the provider puts
    on the wire must carry exactly ``type``/``item``/``value``/``target``.
    """
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.raw(
        "cmd",
        "policy:create",
        value={"type": "mac", "action": "block"},
        target=MAC,
    )
    assert res.ok is True
    assert (cap[-1]["method"], cap[-1]["path"]) == ("POST", "/raw")
    assert cap[-1]["body"] == {
        "type": "cmd",
        "item": "policy:create",
        "value": {"type": "mac", "action": "block"},
        "target": MAC,
    }
    assert cap[-1]["auth"] == "Bearer tok"


def test_raw_omits_optional_value_and_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """type+item are required; value/target are omitted so the bridge fills its defaults.

    The route defaults ``value`` to ``{}`` and ``target`` to ``"0.0.0.0"`` server-side
    (``value || {}`` / ``target || "0.0.0.0"``), so the provider sends only what the
    caller set — no phantom ``value``/``target`` it never meant to specify.
    """
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    p.raw("get", "policies")
    assert cap[-1]["body"] == {"type": "get", "item": "policies"}


def test_raw_bridge_refusal_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, status=500, reply={"error": "boom"})
    p = _provider(monkeypatch)
    assert p.raw("cmd", "policy:create").ok is False


def test_raw_before_connect_raises() -> None:
    from sanctum_cli.devices.base import DeviceError

    p = FirewallaProvider()
    with pytest.raises(DeviceError):
        p.raw("cmd", "policy:create")


# ── rollback: re-based off the REAL primitives (DELETE /policy/:pid + /raw) ─


def _baseline(policies: list[dict[str, Any]]) -> Snapshot:
    """A Snapshot whose /policies baseline is the given policy list (bridge shape)."""
    payload = json.dumps({"policies": policies, "count": len(policies)})
    return Snapshot(brand="firewalla", taken_at="t", data={"/policies": payload})


def test_rollback_deletes_policies_added_since_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A policy present NOW but not in the baseline was added → DELETE /policy/:pid.

    Proves rollback calls the REAL restore primitive (DELETE /policy/:pid) rather
    than the prior silent POST to a non-existent /policies/restore.
    """
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    # Live state has an extra pid 9 that the captured baseline never had.
    live = {"policies": [{"pid": "7"}, {"pid": "9"}], "count": 2}
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda *a, **k: live)
    res = p.rollback(_baseline([{"pid": "7"}]))
    assert res.ok is True
    deletes = [(c["method"], c["path"]) for c in cap if c["method"] == "DELETE"]
    assert ("DELETE", "/policy/9") in deletes  # the added policy is removed
    assert ("DELETE", "/policy/7") not in deletes  # the baseline policy is untouched


def test_rollback_recreates_removed_policies_via_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    """A policy in the baseline but gone NOW was removed → re-create via POST /raw.

    Proves rollback uses the OTHER real primitive (the /raw policy:create escape
    hatch), carrying the captured policy object verbatim as the create value.
    """
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    live = {"policies": [{"pid": "7"}], "count": 1}
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda *a, **k: live)
    removed = {"pid": "5", "type": "mac", "action": "block", "target": MAC}
    res = p.rollback(_baseline([{"pid": "7"}, removed]))
    assert res.ok is True
    raws = [c for c in cap if c["path"] == "/raw"]
    assert raws, "rollback must re-create the removed policy via the /raw escape hatch"
    assert raws[-1]["method"] == "POST"
    assert raws[-1]["body"]["item"] == "policy:create"
    assert raws[-1]["body"]["value"] == removed  # the captured policy object, verbatim


def test_rollback_fail_closed_when_delete_primitive_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the DELETE primitive reports failure, the whole rollback is ok=False.

    Fail-closed: a half-restored device must NOT report a green restore. The bridge
    answers ``500 {success:false}`` on a failed delete → ``delete_policy`` ok=False →
    rollback ok=False (but it DID attempt the real primitive, not a silent no-op).
    """
    cap = _capture(monkeypatch, status=500, reply={"success": False})
    p = _provider(monkeypatch)
    live = {"policies": [{"pid": "7"}, {"pid": "9"}], "count": 2}
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda *a, **k: live)
    res = p.rollback(_baseline([{"pid": "7"}]))
    assert res.ok is False
    assert any(c["method"] == "DELETE" and c["path"] == "/policy/9" for c in cap)


def test_rollback_fail_closed_when_live_state_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge that cannot report the live policy state → fail-closed ok=False.

    Without a live read the diff cannot be computed, so rollback must NOT guess —
    it reports failure (no phantom DELETE/POST) so the rails surface manual recovery.
    """
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda *a, **k: None)  # bridge down
    res = p.rollback(_baseline([{"pid": "7"}]))
    assert res.ok is False
    assert [c for c in cap if c["method"] in ("DELETE", "POST")] == []


def test_rollback_empty_baseline_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty snapshot has no baseline to restore to → ok=False, nothing mutated."""
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    res = p.rollback(Snapshot(brand="firewalla", taken_at="t", data={}))
    assert res.ok is False
    assert cap == []


def test_rollback_noop_when_already_at_baseline_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live state already equal to the baseline → successful restore that mutates nothing.

    The key proof the new rollback is NOT just "always ok=False": when there is
    genuinely nothing to reconcile it reports ok=True and fires no primitive.
    """
    cap = _capture(monkeypatch)
    p = _provider(monkeypatch)
    state = {"policies": [{"pid": "7"}], "count": 1}
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda *a, **k: state)
    res = p.rollback(_baseline([{"pid": "7"}]))
    assert res.ok is True
    assert [c for c in cap if c["method"] in ("DELETE", "POST")] == []


# ── capabilities: data-driven from GET /info (router_mode/enforcement_ready) ─


def test_capabilities_drop_enforcement_when_not_enforcement_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EXPLICIT enforcement_ready=false from /info strips the enforcement caps.

    On a box not in router mode, per-device/feature blocks install but do not
    reliably ENFORCE — advertising them would over-promise. The base (read /
    box-level / local-DNS) caps remain; WAN_MODE is never advertised either way.
    Contract derived from firewalla-bridge.js ``capabilities().enforcement_ready``.
    """
    p = _provider(monkeypatch)
    info = {"capabilities": {"router_mode": False, "enforcement_ready": False}}
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda *a, **k: info)
    caps = p.capabilities()
    for gated in (
        Capability.POLICY,
        Capability.SCREEN_TIME,
        Capability.DEVICE_BLOCK,
        Capability.DEVICE_POLICY,
        Capability.DEVICE_RULES,
        Capability.FEATURE_TOGGLE,
    ):
        assert gated not in caps
    for base in (
        Capability.READ,
        Capability.REBOOT,
        Capability.SPEEDTEST,
        Capability.LOCAL_DNS,
        Capability.ALARM_ACK,
        Capability.WAKE_ON_LAN,
    ):
        assert base in caps
    assert Capability.WAN_MODE not in caps


def test_capabilities_full_set_when_enforcement_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enforcement_ready=true (router mode) → the enforcement caps ARE advertised."""
    p = _provider(monkeypatch)
    info = {"capabilities": {"router_mode": True, "enforcement_ready": True}}
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda *a, **k: info)
    caps = p.capabilities()
    assert Capability.DEVICE_BLOCK in caps
    assert Capability.POLICY in caps
    assert Capability.SCREEN_TIME in caps
    assert Capability.WAN_MODE not in caps


def test_capabilities_permissive_when_info_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown enforcement state (no /info, or no flag) keeps the route-backed caps.

    Only an EXPLICIT enforcement_ready=false strips them; a transient down / older
    bridge that omits the flag must not silently shrink the advertised surface.
    WAN_MODE stays out regardless.
    """
    p = _provider(monkeypatch)  # _provider patches _fetch_bridge_json -> None
    caps = p.capabilities()
    assert Capability.DEVICE_BLOCK in caps
    assert Capability.WAN_MODE not in caps
