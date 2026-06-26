"""Orbi write/action ops + read paths + list_actions() — mocked pynetgear.

Task 5 wraps every pynetgear *write/action* (the ~8 SOAP mutators that ride
``Netgear._set``) as a uniform provider op + capability, adds the read paths for
every getter, and exposes ``list_actions()`` — Orbi's discovery surface, since
pynetgear has NO escape hatch and NO discovery by nature (a FIXED set of 49 SOAP
methods).

CONTRACTS AT THE BOUNDARY (CLAUDE.md) — the central discipline here:

* The fake's method surface is authored from the **library's actual source**
  (``pynetgear/router.py``), NOT from the provider's hopes: every write method
  (``reboot`` / ``update_new_firmware`` / ``set_qos_enable_status`` /
  ``set_smart_connect_enabled`` / ``enable_traffic_meter`` /
  ``set_block_device_enable`` / ``allow_block_device`` / ``set_speed_test_start``)
  rides ``Netgear._set``, which **RETURNS a bool** — ``True`` on success, ``False``
  on a rejected/failed write. It does NOT raise. So "the call did not raise" is
  NOT proof the write landed: the provider MUST fail-close on a falsey return
  (``ok=False``), never report a green ``ok=True`` on a ``False``. That is the
  bug this suite guards (the Orbi analog of Sagemcom's ``_reply_error``).

* The value toggles take a **Python bool** (pynetgear's ``value_to_zero_or_one``
  accepts ``bool`` / ``"1"`` / ``"yes"`` but RAISES ``ValueError`` on the
  provider's own ``"on"``/``"off"`` vocabulary), so the provider must hand a bool
  across the boundary — the cross-layer contract a structural "field exists" test
  would miss.

* ``allow_block_device`` carries a per-MAC arg shape: ``allow_block_device(mac,
  device_status="Block"|"Allow")`` — the only write with a positional + keyword
  shape, asserted against the real signature.

SAFETY: the client factory is mocked, so no socket opens and no live Orbi
(192.168.1.1) is ever touched. Every op is composed behind ``guarded_apply`` at
the intent layer and defaults to dry-run; the overnight build never fires a
write against live gear.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sanctum_cli.devices.base import Capability, Creds, DeviceError


class FakeNetgearActions:
    """Stand-in for ``pynetgear.Netgear`` — write/action + read surface.

    Authored from ``pynetgear/router.py``: every write method rides ``_set`` and
    returns a **bool** (configurable via ``set_ok`` to simulate a rejected write
    so the fail-closed contract is exercised). Reads return the realistic shapes
    the real getters return (a dict, a list of ``Device`` namedtuples, ...). Every
    call is recorded as ``(method, args, kwargs)`` so the boundary tests assert on
    exactly what crossed into the transport.
    """

    def __init__(self, *, set_ok: bool = True, raise_on_call: bool = False) -> None:
        self._set_ok = set_ok
        self._raise = raise_on_call
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.logged_in = False

    def login(self) -> bool:
        self.logged_in = True
        return True

    def get_info(self, use_cache: bool = True) -> dict[str, str]:
        return {"ModelName": "RBR50", "SerialNumber": "ABC123"}

    # ── writes (all ride Netgear._set → return bool) ──────────────────────
    def _record_set(self, name: str, *args: Any, **kwargs: Any) -> bool:
        if self._raise:
            msg = "simulated SOAP transport failure"
            raise RuntimeError(msg)
        self.calls.append((name, args, kwargs))
        return self._set_ok

    def reboot(self) -> bool:
        return self._record_set("reboot")

    def update_new_firmware(self) -> bool:
        return self._record_set("update_new_firmware")

    def set_qos_enable_status(self, value: bool = False) -> bool:
        return self._record_set("set_qos_enable_status", value)

    def set_smart_connect_enabled(self, value: bool = False) -> bool:
        return self._record_set("set_smart_connect_enabled", value)

    def enable_traffic_meter(self, value: bool = False) -> bool:
        return self._record_set("enable_traffic_meter", value)

    def set_block_device_enable(self, value: bool = False) -> bool:
        return self._record_set("set_block_device_enable", value)

    def allow_block_device(self, mac_addr: str, device_status: str = "Block") -> bool:
        return self._record_set("allow_block_device", mac_addr, device_status=device_status)

    def set_speed_test_start(self) -> bool:
        return self._record_set("set_speed_test_start")

    # ── reads ─────────────────────────────────────────────────────────────
    def _record_get(self, name: str, value: Any) -> Any:
        if self._raise:
            msg = "simulated SOAP transport failure"
            raise RuntimeError(msg)
        self.calls.append((name, (), {}))
        return value

    def get_attached_devices(self) -> Any:
        from pynetgear import Device

        dev = Device(
            name="laptop", ip="192.168.1.5", mac="AA:BB:CC:DD:EE:FF", type="wireless",
            signal=88, link_rate=433, allow_or_block="Allow", device_type=None,
            device_model=None, ssid=None, conn_ap_mac=None,
        )
        return self._record_get("get_attached_devices", [dev])

    def get_satellites(self) -> Any:
        return self._record_get(
            "get_satellites", [{"ModelName": "RBS50", "IP": "192.168.1.2"}]
        )

    def get_wan_ip_con_info(self) -> Any:
        return self._record_get(
            "get_wan_ip_con_info",
            {"NewExternalIPAddress": "24.5.6.7", "NewConnectionType": "DHCP"},
        )

    def get_system_info(self) -> Any:
        return self._record_get(
            "get_system_info",
            {"NewCPUUtilization": "12", "NewMemoryUtilization": "30"},
        )

    def get_traffic_meter(self) -> Any:
        # Real get_traffic_meter parses values to float/tuple/timedelta — the
        # timedelta is NOT natively JSON-serializable, which the provider's
        # serializer must survive (default=str), so include one.
        from datetime import timedelta

        return self._record_get(
            "get_traffic_meter",
            {"NewTodayUpload": 6.19, "NewTodayConnectionTime": timedelta(hours=11, minutes=14)},
        )

    def get_traffic_meter_enabled(self) -> Any:
        return self._record_get("get_traffic_meter_enabled", True)

    def get_2g_guest_access_network_info(self) -> Any:
        return self._record_get(
            "get_2g_guest_access_network_info",
            {"NewSSID": "NETGEAR-Guest", "NewSecurityMode": "WPA2"},
        )

    def get_5g_guest_access_network_info(self) -> Any:
        return self._record_get(
            "get_5g_guest_access_network_info",
            {"NewSSID": "NETGEAR-Guest-5G", "NewSecurityMode": "WPA2"},
        )


def _connected(monkeypatch: pytest.MonkeyPatch, fake: FakeNetgearActions) -> Any:
    from sanctum_cli.devices.orbi import OrbiProvider

    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    p = OrbiProvider()
    p.connect(Creds(host="192.168.1.1", username="admin", secret=None, key_path=None))
    return p


# ── write/action ops: each wraps the REAL pynetgear method, fail-closed ──

# (provider method, kwargs, expected pynetgear method, expected positional args)
_VALUE_OPS = [
    ("set_qos", {"enabled": True}, "set_qos_enable_status", (True,)),
    ("set_smart_connect", {"enabled": True}, "set_smart_connect_enabled", (True,)),
    ("set_traffic_meter", {"enabled": True}, "enable_traffic_meter", (True,)),
    ("set_block_device_enable", {"enabled": True}, "set_block_device_enable", (True,)),
]
_BARE_OPS = [
    ("reboot", "reboot"),
    ("update_firmware", "update_new_firmware"),
    ("speed_test", "set_speed_test_start"),
]


@pytest.mark.parametrize(("call", "kwargs", "method", "args"), _VALUE_OPS)
def test_value_op_calls_real_method_with_bool(
    monkeypatch: pytest.MonkeyPatch, call: str, kwargs: dict, method: str, args: tuple
) -> None:
    """A value toggle calls the REAL pynetgear method with a Python bool.

    pynetgear's value_to_zero_or_one RAISES on the provider's "on"/"off"
    vocabulary, so the provider MUST cross the boundary with a bool — asserted
    against exactly what the transport recorded.
    """
    fake = FakeNetgearActions(set_ok=True)
    p = _connected(monkeypatch, fake)
    res = getattr(p, call)(**kwargs)
    assert res.ok is True
    assert (method, args, {}) in fake.calls


@pytest.mark.parametrize(("call", "kwargs", "method", "args"), _VALUE_OPS)
def test_value_op_fail_closes_on_false_return(
    monkeypatch: pytest.MonkeyPatch, call: str, kwargs: dict, method: str, args: tuple
) -> None:
    """A FALSE return (the box rejected the write) must yield ok=False, NOT ok=True.

    Netgear._set returns False on a rejected write WITHOUT raising — the contract
    a structural test misses. The provider fail-closes on it (Orbi analog of
    Sagemcom's _reply_error).
    """
    fake = FakeNetgearActions(set_ok=False)
    p = _connected(monkeypatch, fake)
    res = getattr(p, call)(**kwargs)
    assert res.ok is False  # a False return is NOT a green success
    assert (method, args, {}) in fake.calls  # the write was attempted


@pytest.mark.parametrize(("call", "method"), _BARE_OPS)
def test_bare_op_calls_real_method(
    monkeypatch: pytest.MonkeyPatch, call: str, method: str
) -> None:
    fake = FakeNetgearActions(set_ok=True)
    p = _connected(monkeypatch, fake)
    res = getattr(p, call)()
    assert res.ok is True
    assert (method, (), {}) in fake.calls


@pytest.mark.parametrize(("call", "method"), _BARE_OPS)
def test_bare_op_fail_closes_on_false_return(
    monkeypatch: pytest.MonkeyPatch, call: str, method: str
) -> None:
    fake = FakeNetgearActions(set_ok=False)
    p = _connected(monkeypatch, fake)
    res = getattr(p, call)()
    assert res.ok is False


@pytest.mark.parametrize(("call", "kwargs"), [
    ("reboot", {}),
    ("update_firmware", {}),
    ("speed_test", {}),
    ("set_qos", {"enabled": True}),
    ("set_smart_connect", {"enabled": True}),
    ("set_traffic_meter", {"enabled": True}),
    ("set_block_device_enable", {"enabled": True}),
])
def test_op_raises_deviceerror_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch, call: str, kwargs: dict
) -> None:
    """A transport exception (not a False return) normalizes to DeviceError."""
    fake = FakeNetgearActions(raise_on_call=True)
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        getattr(p, call)(**kwargs)


def test_op_before_connect_raises() -> None:
    from sanctum_cli.devices.orbi import OrbiProvider

    p = OrbiProvider()
    with pytest.raises(DeviceError):
        p.reboot()
    with pytest.raises(DeviceError):
        p.set_qos(enabled=True)


# ── allow_block_device: per-MAC arg shape ─────────────────────────────────


def test_allow_block_device_blocks_with_block_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """allow=False → allow_block_device(mac, device_status='Block') (real signature)."""
    fake = FakeNetgearActions(set_ok=True)
    p = _connected(monkeypatch, fake)
    res = p.allow_block_device("AA:BB:CC:DD:EE:FF", allow=False)
    assert res.ok is True
    assert ("allow_block_device", ("AA:BB:CC:DD:EE:FF",), {"device_status": "Block"}) in fake.calls
    assert "block" in (res.after or "").lower()


def test_allow_block_device_allows_with_allow_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """allow=True → device_status='Allow'."""
    fake = FakeNetgearActions(set_ok=True)
    p = _connected(monkeypatch, fake)
    res = p.allow_block_device("AA:BB:CC:DD:EE:FF", allow=True)
    assert res.ok is True
    assert ("allow_block_device", ("AA:BB:CC:DD:EE:FF",), {"device_status": "Allow"}) in fake.calls


def test_allow_block_device_fail_closes_on_false_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeNetgearActions(set_ok=False)
    p = _connected(monkeypatch, fake)
    res = p.allow_block_device("AA:BB:CC:DD:EE:FF", allow=False)
    assert res.ok is False


def test_allow_block_device_transport_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeNetgearActions(raise_on_call=True)
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.allow_block_device("AA:BB:CC:DD:EE:FF", allow=False)


# ── generic action(name, **kwargs) dispatcher ─────────────────────────────


def test_action_dispatches_named_value_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """action('set_qos', value=True) reaches the real method with the bool."""
    fake = FakeNetgearActions(set_ok=True)
    p = _connected(monkeypatch, fake)
    res = p.action("set_qos", value=True)
    assert res.ok is True
    assert ("set_qos_enable_status", (True,), {}) in fake.calls


def test_action_dispatches_named_mac_op(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNetgearActions(set_ok=True)
    p = _connected(monkeypatch, fake)
    res = p.action("allow_block_device", mac="AA:BB:CC:DD:EE:FF", allow=False)
    assert res.ok is True
    assert ("allow_block_device", ("AA:BB:CC:DD:EE:FF",), {"device_status": "Block"}) in fake.calls


def test_action_unknown_name_returns_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown action name is refused (ok=False) — Orbi has NO escape hatch, so
    a name outside the fixed wired set cannot be dispatched to the wire."""
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    res = p.action("set_port_forward", value=True)  # no such SOAP method exists
    assert res.ok is False
    assert fake.calls == []  # nothing reached the transport


def test_action_before_connect_raises() -> None:
    from sanctum_cli.devices.orbi import OrbiProvider

    p = OrbiProvider()
    with pytest.raises(DeviceError):
        p.action("reboot")


# ── list_actions(): Orbi's discovery surface ──────────────────────────────


def test_list_actions_enumerates_wired_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_actions() enumerates exactly the wired pynetgear write/action methods."""
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    actions = p.list_actions()
    names = {a.name for a in actions}
    assert names == {
        "reboot",
        "update_firmware",
        "set_qos",
        "set_smart_connect",
        "set_traffic_meter",
        "set_block_device_enable",
        "allow_block_device",
        "speed_test",
    }


def test_list_actions_methods_exist_on_real_pynetgear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every method list_actions() names MUST exist on the real Netgear class.

    The strongest Contracts-at-the-Boundary check for the discovery surface: a
    list_actions() that named a method the library does not expose would be a
    phantom — assert against the REAL pynetgear class, not the fake.
    """
    pynetgear = pytest.importorskip("pynetgear")
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    for action in p.list_actions():
        assert hasattr(pynetgear.Netgear, action.method), (
            f"list_actions names {action.method!r} which Netgear does not expose"
        )


def test_list_actions_capabilities_are_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every action's capability is one the provider actually advertises."""
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    caps = p.capabilities()
    for action in p.list_actions():
        assert action.capability in caps


# ── capabilities: honest-verify (cap iff a real op backs it) ──────────────


def test_capabilities_advertise_every_backed_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    caps = p.capabilities()
    assert Capability.REBOOT in caps
    assert Capability.FIRMWARE in caps
    assert Capability.SPEEDTEST in caps
    assert Capability.POLICY in caps
    assert Capability.FEATURE_TOGGLE in caps
    assert Capability.GUEST_WIFI in caps
    assert Capability.READ in caps


def test_capabilities_omit_ap_mode_and_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Honesty defect guard: AP_MODE + CHANNELS have NO pynetgear SOAP write, so
    they are NEVER advertised (no set-AP-mode, no set-channel verb)."""
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    caps = p.capabilities()
    assert Capability.AP_MODE not in caps
    assert Capability.CHANNELS not in caps


def test_capabilities_invariant_every_write_cap_has_a_backing_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest-verify invariant: every advertised cap (besides READ/GUEST_WIFI,
    which are backed by getters / the capability_op) is backed by ≥1 wired action,
    so capabilities() and the action set can never drift into a phantom cap."""
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    backed = {a.capability for a in p.list_actions()}
    for cap in p.capabilities():
        if cap in (Capability.READ, Capability.GUEST_WIFI):
            continue
        assert cap in backed, f"capability {cap} advertised with no backing action"


# ── read paths: every getter, JSON-serialized ─────────────────────────────


def test_read_attached_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    raw = p.get("devices/attached")
    assert raw is not None
    data = json.loads(raw)
    assert isinstance(data, list)
    assert data[0]["mac"] == "AA:BB:CC:DD:EE:FF"
    assert data[0]["ip"] == "192.168.1.5"


def test_read_satellites(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    data = json.loads(p.get("mesh/satellites") or "null")
    assert data[0]["ModelName"] == "RBS50"


def test_read_wan_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    data = json.loads(p.get("wan/ip") or "null")
    assert data["NewExternalIPAddress"] == "24.5.6.7"


def test_read_system_info(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    data = json.loads(p.get("system/info") or "null")
    assert data["NewCPUUtilization"] == "12"


def test_read_traffic_meter_survives_non_json_native_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The traffic-meter read serializes even a timedelta value (default=str)."""
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    raw = p.get("traffic/meter")
    assert raw is not None
    data = json.loads(raw)
    assert "NewTodayUpload" in data
    # the timedelta crossed into a JSON string rather than blowing up serialization
    assert data["NewTodayConnectionTime"]


def test_read_guest_info(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNetgearActions()
    p = _connected(monkeypatch, fake)
    data = json.loads(p.get("guest_wifi/5g/info") or "null")
    assert data["NewSSID"] == "NETGEAR-Guest-5G"


def test_read_returns_none_when_getter_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A getter returning None (read error inside pynetgear) → provider get None."""
    fake = FakeNetgearActions()

    def none_getter() -> None:
        return None

    fake.get_satellites = none_getter  # type: ignore[method-assign]
    p = _connected(monkeypatch, fake)
    assert p.get("mesh/satellites") is None


def test_read_raises_deviceerror_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeNetgearActions(raise_on_call=True)
    p = _connected(monkeypatch, fake)
    with pytest.raises(DeviceError):
        p.get("wan/ip")


def test_read_path_before_connect_raises() -> None:
    from sanctum_cli.devices.orbi import OrbiProvider

    p = OrbiProvider()
    with pytest.raises(DeviceError):
        p.get("wan/ip")


# ── real-pynetgear SOAP-body boundary (importorskip, socket-mocked) ───────


def _capture_post(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
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


def test_set_qos_lands_in_real_soap_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """END-TO-END: provider.set_qos(True) → real pynetgear → recorded SOAP body.

    Drives the provider's op through the REAL pynetgear ``set_qos_enable_status``
    with only the socket mocked, asserting the bool→'1' normalization lands as
    ``<NewQoSEnable>1`` on the wire — the cross-layer contract proven against the
    recorded SOAP request, not a field read before encoding.
    """
    import re

    pynetgear = pytest.importorskip("pynetgear")
    from sanctum_cli.devices.orbi import OrbiProvider

    captured = _capture_post(monkeypatch)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")

    def real_client(creds: Creds) -> object:
        client = pynetgear.Netgear(
            password=creds.secret or "", host=creds.host, user=creds.username
        )
        client.cookie = "c"  # test seam: skip the login socket
        return client

    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", real_client)

    p = OrbiProvider()
    try:
        p.connect(Creds(host="192.168.1.1", username="admin", secret=None, key_path=None))
        p._client.cookie = "c"  # type: ignore[union-attr]  # authed-session seam
        captured.clear()
        res = p.set_qos(enabled=True)
    finally:
        p.disconnect()

    assert res.ok is True
    bodies = [b for _u, b in captured if "SetQoSEnableStatus" in b]
    assert bodies, "set_qos never reached a real SetQoSEnableStatus SOAP call"
    match = re.search(r"<NewQoSEnable>(.*?)</NewQoSEnable>", bodies[-1])
    assert match is not None
    assert match.group(1) == "1"  # True → pynetgear bool→'1'


def test_allow_block_device_mac_rides_verbatim_into_soap_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-MAC arg + Block status ride verbatim into the SOAP body.

    pynetgear interpolates params raw into XML (no URL-encoding), so the contract
    is verbatim pass-through: the MAC and 'Block' land as-is on the wire — proven
    against the real SetBlockDeviceByMAC SOAP request.
    """
    import re

    pynetgear = pytest.importorskip("pynetgear")
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

    mac = "AA:BB:CC:DD:EE:FF"
    p = OrbiProvider()
    try:
        p.connect(Creds(host="192.168.1.1", username="admin", secret=None, key_path=None))
        p._client.cookie = "c"  # type: ignore[union-attr]
        captured.clear()
        res = p.allow_block_device(mac, allow=False)
    finally:
        p.disconnect()

    assert res.ok is True
    bodies = [b for _u, b in captured if "SetBlockDeviceByMAC" in b]
    assert bodies, "allow_block_device never reached a real SetBlockDeviceByMAC SOAP call"
    body = bodies[-1]
    mac_match = re.search(r"<NewMACAddress>(.*?)</NewMACAddress>", body)
    status_match = re.search(r"<NewAllowOrBlock>(.*?)</NewAllowOrBlock>", body)
    assert mac_match is not None and mac_match.group(1) == mac  # verbatim, not encoded
    assert status_match is not None and status_match.group(1) == "Block"
