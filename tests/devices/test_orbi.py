"""Orbi (NETGEAR) provider — mocked pynetgear client, no network.

The Orbi provider drives a NETGEAR Orbi mesh router through the maintained
``pynetgear`` SOAP transport. These tests mock the client factory
(``_make_client``) and the keychain so no socket is ever opened and no Keychain
prompt fires. Nothing here touches a live Orbi; the live read-only smoke is a
separate, env-gated test (``SANCTUM_LIVE_ORBI=1``) that does a single
``get_info`` and never mutates.

SAFETY: mutating ops (``set`` of guest-wifi / channels, the ``snapshot`` /
``rollback`` restore path) are exercised only against the mocked client; the
overnight build never fires a write against live gear. The provider's own
``set`` returns an ``OpResult`` and is composed behind ``guarded_apply``
(dry-run / guarded_apply rails) at the intent layer — never auto-fired.

``pynetgear`` is imported LAZILY inside the provider's ``_make_client`` factory,
so importing the provider module never requires the optional transport, and
these tests mock that factory and never import the real library at all.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from sanctum_cli.devices.base import Capability, Creds, DeviceError, NetContext

# Provider path vocabulary (stable, brand-owned leaf addresses).
GUEST_2G = "guest_wifi/2g"
GUEST_5G = "guest_wifi/5g"
CHANNEL_2G = "channel/2g"
CHANNEL_5G = "channel/5g"


class FakeNetgear:
    """Stand-in for ``pynetgear.Netgear``.

    Exposes the exact surface the provider calls: ``login`` plus the guest-access
    getters/setters, the per-band info getters (for channel state), ``get_info``
    (for brand refinement), and ``check_new_firmware``. Getter reads come from an
    in-memory map; setters record every call so boundary/encoding tests can assert
    on exactly what the transport received and flip the read-back state.

    ``login`` returns True by default; pass ``login_ok=False`` to simulate an auth
    failure (the provider must tolerate it at connect but RAISE on a later get).
    Set ``raise_on_get=True`` to simulate a transport error on a read so the
    DeviceError-on-failure contract can be exercised.
    """

    def __init__(
        self,
        *,
        guest_2g: bool = False,
        guest_5g: bool = False,
        channel_2g: str = "6",
        channel_5g: str = "44",
        model: str = "RBR50",
        login_ok: bool = True,
        raise_on_get: bool = False,
    ) -> None:
        self._guest = {"2g": guest_2g, "5g": guest_5g}
        self._channel = {"2g": channel_2g, "5g": channel_5g}
        self._model = model
        self._login_ok = login_ok
        self._raise_on_get = raise_on_get
        self.logged_in = False
        self.set_calls: list[tuple[str, Any]] = []

    def login(self) -> bool:
        self.logged_in = self._login_ok
        return self._login_ok

    def _guard(self) -> None:
        if self._raise_on_get:
            msg = "simulated SOAP transport failure"
            raise RuntimeError(msg)

    def get_info(self, use_cache: bool = True) -> dict[str, str] | None:
        self._guard()
        return {"ModelName": self._model, "SerialNumber": "ABC123"}

    def get_2g_guest_access_enabled(self) -> bool:
        self._guard()
        return self._guest["2g"]

    def get_5g_guest_access_enabled(self) -> bool:
        self._guard()
        return self._guest["5g"]

    def set_2g_guest_access_enabled(self, value: bool = False) -> bool:
        self.set_calls.append((GUEST_2G, value))
        self._guest["2g"] = value
        return True

    def set_5g_guest_access_enabled(self, value: bool = False) -> bool:
        self.set_calls.append((GUEST_5G, value))
        self._guest["5g"] = value
        return True

    def get_2g_info(self) -> dict[str, str] | None:
        self._guard()
        return {"Channel": self._channel["2g"]}

    def get_5g_info(self) -> dict[str, str] | None:
        self._guard()
        return {"Channel": self._channel["5g"]}

    def check_new_firmware(self) -> dict[str, str] | None:
        self._guard()
        return {"CurrentVersion": "V2.7.3", "NewVersion": ""}


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> FakeNetgear:
    """A fake pynetgear client, with keychain + factory mocked."""
    fake = FakeNetgear()
    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    return fake


def _connected(monkeypatch: pytest.MonkeyPatch, fake: FakeNetgear) -> Any:
    from sanctum_cli.devices.orbi import OrbiProvider

    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", lambda creds: fake)
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    p = OrbiProvider()
    p.connect(Creds(host="192.168.1.1", username="admin", secret=None, key_path=None))
    return p


# ── connect: keychain pw + lazy client + brand refine ─────────────────


def test_connect_reads_password_from_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect() must read the admin pw from the documented keychain tuple."""
    seen: dict[str, str] = {}

    def fake_read(account: str, service: str) -> str:
        seen["account"] = account
        seen["service"] = service
        return "pw"

    fake = FakeNetgear()
    captured: dict[str, Creds] = {}

    def fake_make_client(creds: Creds) -> FakeNetgear:
        captured["creds"] = creds
        return fake

    monkeypatch.setattr("sanctum_cli.keychain.read", fake_read)
    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", fake_make_client)

    from sanctum_cli.devices.orbi import OrbiProvider

    p = OrbiProvider()
    p.connect(Creds(host="192.168.1.1", username="admin", secret=None, key_path=None))
    assert seen == {"account": "admin", "service": "orbi-admin"}
    # The secret read from keychain must reach the client factory.
    assert captured["creds"].secret == "pw"


def test_connect_refines_brand_from_model(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    assert p.brand == "orbi-rbr50"


def test_connect_keeps_generic_brand_when_model_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeNetgear(model="")
    p = _connected(monkeypatch, fake)
    assert p.brand == "orbi"


def test_connect_tolerates_login_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed login must NOT blow up connect — brand stays generic (best-effort)."""
    fake = FakeNetgear(login_ok=False)
    p = _connected(monkeypatch, fake)
    # connect tolerated the failed login; brand could not be refined.
    assert p.brand == "orbi"


# ── auth_ok: the positive auth oracle for a best-effort connect ───────


def test_auth_ok_true_after_successful_login(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine login (login_ok=True) → auth_ok() reports the session authed."""
    p = _connected(monkeypatch, patched)
    assert p.auth_ok() is True


def test_auth_ok_false_when_login_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A REJECTED login (login() → False) → auth_ok() is False even though connect
    did NOT raise. This is the contract the onboard pairing gate relies on to
    fail-close: connect is best-effort, so only auth_ok positively confirms auth.
    """
    fake = FakeNetgear(login_ok=False)
    p = _connected(monkeypatch, fake)
    assert p.auth_ok() is False


def test_auth_ok_false_when_login_raises_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An UNREACHABLE box (login() raises) → connect tolerates it, auth_ok() is False."""
    from sanctum_cli.devices.orbi import OrbiProvider

    class _Unreachable:
        def login(self) -> bool:
            msg = "no route to host"
            raise OSError(msg)

    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", lambda creds: _Unreachable())
    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    p = OrbiProvider()
    p.connect(Creds(host="192.168.1.1", username="admin", secret=None, key_path=None))  # no raise
    assert p.auth_ok() is False


def test_auth_ok_false_before_connect() -> None:
    """A fresh provider has not authenticated — auth_ok() is False."""
    from sanctum_cli.devices.orbi import OrbiProvider

    assert OrbiProvider().auth_ok() is False


def test_disconnect_resets_auth_ok(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    """disconnect() drops the authed flag so a stale session never reads as authed."""
    p = _connected(monkeypatch, patched)
    assert p.auth_ok() is True
    p.disconnect()
    assert p.auth_ok() is False


def test_connect_none_creds_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect(None) is not allowed — the provider needs host/username."""
    from sanctum_cli.devices.orbi import OrbiProvider

    monkeypatch.setattr("sanctum_cli.keychain.read", lambda account, service: "pw")
    p = OrbiProvider()
    with pytest.raises(DeviceError):
        p.connect(None)


# ── get / set: guest-wifi + channels ──────────────────────────────────


def test_get_guest_wifi_enabled(patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch) -> None:
    patched._guest["5g"] = True
    p = _connected(monkeypatch, patched)
    assert p.get(GUEST_5G) == "on"
    assert p.get(GUEST_2G) == "off"


def test_get_channel(patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _connected(monkeypatch, patched)
    assert p.get(CHANNEL_2G) == "6"
    assert p.get(CHANNEL_5G) == "44"


def test_get_returns_none_for_unknown_path(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    assert p.get("orbi/does-not-exist") is None


def test_set_guest_wifi_records_opresult_before_after(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    res = p.set(GUEST_5G, "on")
    assert res.ok is True
    assert res.before == "off"
    assert res.after == "on"
    assert (GUEST_5G, True) in patched.set_calls


def test_set_then_get_reflects_change(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    p.set(GUEST_2G, "on")
    assert p.get(GUEST_2G) == "on"


def test_get_raises_deviceerror_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get() must RAISE DeviceError on a transport failure (per the Protocol)."""
    fake = FakeNetgear()
    p = _connected(monkeypatch, fake)
    # Flip the fake to fail every subsequent read.
    fake._raise_on_get = True
    with pytest.raises(DeviceError):
        p.get(GUEST_5G)


def test_op_before_connect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Using the provider before connect() must fail legibly, not AttributeError."""
    from sanctum_cli.devices.orbi import OrbiProvider

    p = OrbiProvider()
    with pytest.raises(DeviceError):
        p.get(GUEST_5G)


# ── capabilities + capability_op ──────────────────────────────────────


def test_capabilities_advertise_orbi_surface(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    caps = p.capabilities()
    assert Capability.READ in caps
    assert Capability.FIRMWARE in caps
    assert Capability.AP_MODE in caps
    assert Capability.CHANNELS in caps
    assert Capability.GUEST_WIFI in caps


def test_capability_op_maps_guest_wifi(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    """capability_op(GUEST_WIFI) returns the Orbi (path, engaged) the intent uses."""
    p = _connected(monkeypatch, patched)
    op = p.capability_op(Capability.GUEST_WIFI)
    assert op is not None
    assert op.path == GUEST_5G
    assert op.engaged == "on"


def test_capability_op_none_for_unsupported(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capability the provider does not bind returns None (no blind mutation)."""
    p = _connected(monkeypatch, patched)
    assert p.capability_op(Capability.BRIDGE_MODE) is None


# ── snapshot / rollback ───────────────────────────────────────────────


def test_snapshot_captures_guest_and_channel_state(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched._guest["5g"] = True
    p = _connected(monkeypatch, patched)
    snap = p.snapshot()
    assert snap.brand.startswith("orbi")
    assert snap.data[GUEST_5G] == "on"
    assert snap.data[GUEST_2G] == "off"
    assert snap.data[CHANNEL_2G] == "6"
    assert snap.taken_at  # ISO-8601 stamp present


def test_rollback_restores_changed_keys(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    snap = p.snapshot()
    p.set(GUEST_5G, "on")
    patched.set_calls.clear()
    res = p.rollback(snap)
    assert res.ok is True
    # rollback re-issues a setter restoring the captured value.
    assert (GUEST_5G, False) in patched.set_calls


def test_rollback_empty_snapshot_reports_failure(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty-baseline rollback must report ok=False — not a silent success."""
    from sanctum_cli.devices.base import Snapshot

    p = _connected(monkeypatch, patched)
    empty = Snapshot(brand="orbi", taken_at="t", data={})
    res = p.rollback(empty)
    assert res.ok is False  # nothing to restore is a FAILED rollback
    assert patched.set_calls == []  # and it issued no writes


def test_rollback_with_baseline_reports_success(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollback that restores at least one leaf reports ok=True."""
    p = _connected(monkeypatch, patched)
    snap = p.snapshot()
    res = p.rollback(snap)
    assert res.ok is True


# ── detect ────────────────────────────────────────────────────────────


def test_detect_returns_one_when_orbi(monkeypatch: pytest.MonkeyPatch) -> None:
    """detect() probes the gateway read-only; an Orbi banner → 1.0."""
    from sanctum_cli.devices import orbi

    monkeypatch.setattr(orbi, "_probe_is_orbi", lambda gateway_ip: True)
    net = NetContext(gateway_ip="192.168.1.1", runner=None)
    assert orbi.OrbiProvider.detect(net) == 1.0


def test_detect_zero_when_no_gateway() -> None:
    from sanctum_cli.devices import orbi

    net = NetContext(gateway_ip=None, runner=None)
    assert orbi.OrbiProvider.detect(net) == 0.0


def test_detect_zero_when_not_orbi(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanctum_cli.devices import orbi

    monkeypatch.setattr(orbi, "_probe_is_orbi", lambda gateway_ip: False)
    net = NetContext(gateway_ip="10.0.0.1", runner=None)
    assert orbi.OrbiProvider.detect(net) == 0.0


# ── _probe_is_orbi: real read-only fingerprint (injected http_get seam) ──
#
# The fingerprint GETs the unauthenticated NETGEAR ``currentsetting.htm`` banner
# and matches a ``Model=RB*`` line. The http_get seam is injected so no socket
# opens; the default getter (httpx) is exercised only at the live boundary. Pure
# read — no mutation, no auth.


def test_probe_is_orbi_matches_currentsetting_model_banner() -> None:
    from sanctum_cli.devices import orbi

    body = "Firmware=V2.7.3.22\nModel=RBR750\nRegionTag=..."  # Orbi currentsetting.htm
    assert orbi._probe_is_orbi("10.0.0.1", http_get=lambda url: body) is True


def test_probe_is_orbi_false_on_foreign_or_dead() -> None:
    from sanctum_cli.devices import orbi

    assert (
        orbi._probe_is_orbi("192.168.2.1", http_get=lambda url: "XMO_INVALID_SESSION_ERR") is False
    )

    def boom(url):
        raise OSError("refused")

    assert orbi._probe_is_orbi("192.168.2.1", http_get=boom) is False


# ── registration + lifecycle ──────────────────────────────────────────


def test_provider_registered_under_orbi() -> None:
    """Importing the module self-registers the provider under kind=orbi."""
    from sanctum_cli.devices import orbi, registry

    assert "orbi" in registry._REGISTRY
    assert orbi.OrbiProvider in registry._REGISTRY["orbi"]


def test_provider_is_structurally_a_device_provider(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanctum_cli.devices.base import DeviceProvider

    p = _connected(monkeypatch, patched)
    assert isinstance(p, DeviceProvider)  # runtime_checkable structural conformance


def test_disconnect_is_idempotent_and_safe_unconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect() is a no-op when never connected and safe to call twice."""
    from sanctum_cli.devices.orbi import OrbiProvider

    p = OrbiProvider()
    p.disconnect()  # never connected: must not raise
    p.disconnect()  # twice: still safe
    assert p._client is None


def test_disconnect_drops_client(
    patched: FakeNetgear, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _connected(monkeypatch, patched)
    assert p._client is not None
    p.disconnect()
    assert p._client is None


# ── live read-only smoke (env-gated, default-skipped) ─────────────────

LIVE_ORBI = os.environ.get("SANCTUM_LIVE_ORBI") == "1"


@pytest.mark.skipif(
    not LIVE_ORBI,
    reason=(
        "live Orbi smoke is opt-in: set SANCTUM_LIVE_ORBI=1 to run "
        "(read-only get_info, no mutation)"
    ),
)
def test_live_orbi_read_only_info() -> None:
    """Read-only smoke against the REAL Orbi — opt-in, never mutates.

    Connects with the keychain admin pw and reads ``get_info`` via a provider
    ``get``; asserts a non-empty model. NO ``set`` / guest-wifi / channel
    mutation is performed. Skipped unless ``SANCTUM_LIVE_ORBI=1`` so the default
    gate (CI / overnight build / clean checkout) never opens a socket to live
    gear. We have no live Orbi creds yet, so this is deferred — never block on it.
    """
    from sanctum_cli.devices.orbi import OrbiProvider

    p = OrbiProvider()
    try:
        p.connect(Creds(host="192.168.1.1", username="admin", secret=None, key_path=None))
        model = p.get("info/model")
    finally:
        p.disconnect()
    assert model, "live Orbi returned an empty model"
