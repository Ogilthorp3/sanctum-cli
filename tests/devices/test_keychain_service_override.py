"""Cross-layer contract: a devices.<kind>.keychain.service override reaches the
provider's Keychain password read — not just the login username.

CLAUDE.md "Contracts at the Boundary": a structural assertion that
``device_creds`` carries the resolved ``(service, account)`` only proves the
field exists. It does NOT prove the *downstream consumer* (the provider's
``connect`` → ``keychain.read``) actually reads the password under that resolved
tuple. The prior shipped state passed every field-level test yet the password
lookup still used the provider's hardcoded brand constants — so an override of
``devices.hub.keychain.service`` was DEAD (resolved, asserted, never consumed).

These tests close that gap by feeding the resolved ``Creds`` THROUGH the real
``provider.connect`` and asserting on the ``(account, service)`` that reached
``keychain.read``. Only the genuinely-expensive layers are mocked — the client
factory (``_make_client``, no socket) and ``keychain.read`` (no Keychain prompt).
The resolver → Creds → provider read path is exercised for real.

The expectations are derived from a DIFFERENT source than the production code
(the override values the test itself sets), not from the brand constants — so a
shared-assumption bug (test and code both reading ``KEYCHAIN_SERVICE``) cannot
hide here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.commands import net
from sanctum_cli.devices.base import NetContext
from sanctum_cli.devices.orbi import OrbiProvider
from sanctum_cli.devices.sagemcom import SagemcomHubProvider

if TYPE_CHECKING:
    import pytest


def _net(gateway_ip: str = "192.168.2.1") -> NetContext:
    return NetContext(gateway_ip=gateway_ip, runner=None)


def _stub_instance(monkeypatch: pytest.MonkeyPatch, values: dict[str, object]) -> None:
    monkeypatch.setattr(
        "sanctum_cli.commands.net.config.instance_value",
        lambda key, default=None: values.get(key, default),
    )


def _capture_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Record the (account, service) every keychain.read receives; return 'pw'."""
    seen: dict[str, str] = {}

    def fake_read(account: str, service: str) -> str:
        seen["account"] = account
        seen["service"] = service
        return "pw"

    monkeypatch.setattr("sanctum_cli.keychain.read", fake_read)
    return seen


# ── Sagemcom hub ──────────────────────────────────────────────────────


def test_hub_service_override_reaches_keychain_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A devices.hub.keychain.{service,account} override is the tuple the password
    is READ under — closing the finding: the override is no longer dead."""

    class _FakeSah:
        async def login(self) -> None: ...
        async def get_value_by_xpath(self, xpath: str, options: object = None) -> None:
            return None

    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: _FakeSah())
    seen = _capture_keychain(monkeypatch)
    _stub_instance(
        monkeypatch,
        {
            "devices.hub.keychain.service": "my-router-admin",
            "devices.hub.keychain.account": "operator",
        },
    )

    creds = net.device_creds("hub", _net())
    provider = SagemcomHubProvider()
    try:
        provider.connect(creds)
    finally:
        provider.disconnect()

    # The password lookup used the RESOLVED override, NOT the brand constants
    # (bell-hub-admin / admin).
    assert seen == {"account": "operator", "service": "my-router-admin"}


def test_hub_default_reaches_brand_keychain_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """With NOTHING configured, the default resolves to the brand tuple — the
    default path is unchanged (no regression)."""

    class _FakeSah:
        async def login(self) -> None: ...
        async def get_value_by_xpath(self, xpath: str, options: object = None) -> None:
            return None

    monkeypatch.setattr("sanctum_cli.devices.sagemcom._make_client", lambda creds: _FakeSah())
    seen = _capture_keychain(monkeypatch)
    _stub_instance(monkeypatch, {})

    creds = net.device_creds("hub", _net())
    provider = SagemcomHubProvider()
    try:
        provider.connect(creds)
    finally:
        provider.disconnect()

    assert seen == {"account": "admin", "service": "bell-hub-admin"}


# ── Orbi mesh ─────────────────────────────────────────────────────────


def test_orbi_service_override_reaches_keychain_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A devices.orbi.keychain.{service,account} override is the tuple the password
    is READ under — the Orbi half of the same fix."""

    class _FakeNetgear:
        def login(self) -> bool:
            return False  # best-effort; no brand refine, no extra reads

    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", lambda creds: _FakeNetgear())
    seen = _capture_keychain(monkeypatch)
    _stub_instance(
        monkeypatch,
        {
            "devices.orbi.keychain.service": "mesh-admin",
            "devices.orbi.keychain.account": "netadmin",
        },
    )

    creds = net.device_creds("orbi", _net("192.168.1.1"))
    provider = OrbiProvider()
    try:
        provider.connect(creds)
    finally:
        provider.disconnect()

    assert seen == {"account": "netadmin", "service": "mesh-admin"}


def test_orbi_default_reaches_brand_keychain_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """With NOTHING configured, the Orbi default resolves to its brand tuple."""

    class _FakeNetgear:
        def login(self) -> bool:
            return False

    monkeypatch.setattr("sanctum_cli.devices.orbi._make_client", lambda creds: _FakeNetgear())
    seen = _capture_keychain(monkeypatch)
    _stub_instance(monkeypatch, {})

    creds = net.device_creds("orbi", _net("192.168.1.1"))
    provider = OrbiProvider()
    try:
        provider.connect(creds)
    finally:
        provider.disconnect()

    assert seen == {"account": "admin", "service": "orbi-admin"}
