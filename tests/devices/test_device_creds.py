"""``net.device_creds`` — generalized, discovery-first device credential resolution.

The helper closes the prior low finding that ``_hub_creds`` hardcoded the Sagemcom
keychain tuple: a credential's (service, account) must come from instance.yaml
(``devices.<kind>.keychain.{service,account}``) when set, falling back to a
per-kind default otherwise. The ``account`` becomes the Creds ``username``; the
``secret`` is always ``None`` (the provider re-reads the password/token from the
Keychain at connect time — credentials never flow through the CLI layer).

These tests drive ``config.instance_value`` through a monkeypatch so no real
instance.yaml / Keychain is touched, and the per-kind defaults are asserted
explicitly (Bert is dyslexic — the old→new tuple is stated, never eyeballed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanctum_cli.commands import net
from sanctum_cli.devices.base import Creds, NetContext

if TYPE_CHECKING:
    import pytest


def _net(gateway_ip: str | None = "192.168.2.1") -> NetContext:
    return NetContext(gateway_ip=gateway_ip, runner=None)


def _stub_instance(monkeypatch: pytest.MonkeyPatch, values: dict[str, object]) -> None:
    """Make ``net.config.instance_value`` answer from an in-memory dict."""
    monkeypatch.setattr(
        "sanctum_cli.commands.net.config.instance_value",
        lambda key, default=None: values.get(key, default),
    )


def test_device_creds_hub_default_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """hub with NOTHING configured → service=bell-hub-admin, username/account=admin."""
    _stub_instance(monkeypatch, {})
    creds = net.device_creds("hub", _net())
    assert isinstance(creds, Creds)
    assert creds.host == "192.168.2.1"
    assert creds.username == "admin"  # the default account
    assert creds.secret is None  # provider self-resolves from Keychain
    assert creds.key_path is None


def test_device_creds_host_override_beats_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured ``devices.<kind>.host`` wins over the local default gateway.

    The hub is the Firewalla's WAN-side gateway (e.g. the Sagemcom at 192.168.2.1),
    NOT the local default gateway (which from a LAN client is the Firewalla at
    10.0.0.1). Without this override the tool targets the wrong host. The override
    is the explicit fix the live pre-flight required.
    """
    _stub_instance(monkeypatch, {"devices.hub.host": "192.168.2.1"})
    creds = net.device_creds("hub", _net("10.0.0.1"))  # local gateway is the Firewalla
    assert creds.host == "192.168.2.1"  # the configured hub host, not 10.0.0.1


def test_device_creds_host_falls_back_to_gateway_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no host override, host stays the detected gateway (unchanged default)."""
    _stub_instance(monkeypatch, {})
    creds = net.device_creds("hub", _net("10.0.0.1"))
    assert creds.host == "10.0.0.1"


def test_device_creds_orbi_default_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """orbi with NOTHING configured → service=orbi-admin, username/account=admin."""
    _stub_instance(monkeypatch, {})
    creds = net.device_creds("orbi", _net("192.168.1.1"))
    assert creds.host == "192.168.1.1"
    assert creds.username == "admin"  # the default account
    assert creds.secret is None


def test_device_creds_account_override_becomes_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An instance.yaml devices.<kind>.keychain.account overrides the username."""
    _stub_instance(
        monkeypatch,
        {"devices.hub.keychain.account": "operator"},
    )
    creds = net.device_creds("hub", _net())
    assert creds.username == "operator"  # NOT the default "admin"


def test_device_creds_service_override_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """An instance.yaml devices.<kind>.keychain.service is the resolved service."""
    captured: dict[str, str] = {}
    _stub_instance(
        monkeypatch,
        {"devices.hub.keychain.service": "my-router-admin"},
    )
    # device_creds exposes the resolved (service, account) via keychain_ref so the
    # value is testable without reaching into the provider's connect path.
    service, account = net.device_keychain_ref("hub")
    captured["service"] = service
    captured["account"] = account
    assert captured["service"] == "my-router-admin"  # the override
    assert captured["account"] == "admin"  # default account still applies


def test_device_creds_threads_resolved_service_into_keychain_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved service reaches Creds.keychain_service (not just the account).

    The prior shape discarded the resolved service: only the account flowed out
    (as the username). This asserts the OTHER half of the tuple is now carried so
    the provider can read the password under the override, not the brand constant.
    """
    _stub_instance(
        monkeypatch,
        {
            "devices.hub.keychain.service": "my-router-admin",
            "devices.hub.keychain.account": "operator",
        },
    )
    creds = net.device_creds("hub", _net())
    assert creds.username == "operator"  # resolved account → login user
    assert creds.keychain_service == "my-router-admin"  # resolved service → carried
    assert creds.secret is None  # provider self-resolves from Keychain


def test_device_creds_default_keychain_service_is_per_kind_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing configured, keychain_service is the per-kind default service."""
    _stub_instance(monkeypatch, {})
    assert net.device_creds("hub", _net()).keychain_service == "bell-hub-admin"
    assert net.device_creds("orbi", _net("192.168.1.1")).keychain_service == "orbi-admin"


def test_device_creds_unknown_kind_keychain_service_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown kind resolves to an empty service → carried as None, never another
    kind's service. None makes the provider fall back to ITS OWN per-brand default
    rather than silently reading a wrong entry."""
    _stub_instance(monkeypatch, {})
    creds = net.device_creds("printer", _net())
    assert creds.keychain_service is None


def test_device_keychain_ref_orbi_defaults() -> None:
    """device_keychain_ref('orbi') with no config → (orbi-admin, admin)."""
    # No instance.yaml on a fresh box — instance_value returns the default, so the
    # ref must be the per-kind fallback tuple. Uses the real config.instance_value
    # against a (presumed-absent) file path; a fresh CI box has no devices block.
    service, account = net.device_keychain_ref("orbi", instance_lookup=lambda _k, d=None: d)
    assert (service, account) == ("orbi-admin", "admin")


def test_device_keychain_ref_hub_defaults() -> None:
    """device_keychain_ref('hub') with no config → (bell-hub-admin, admin)."""
    service, account = net.device_keychain_ref("hub", instance_lookup=lambda _k, d=None: d)
    assert (service, account) == ("bell-hub-admin", "admin")


def test_hub_creds_honors_account_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """_hub_creds must route through device_creds — an account override reaches it.

    Closes the prior low finding that ``_hub_creds`` hardcoded the Sagemcom
    keychain tuple (so a haus whose hub admin account is not literally "admin"
    could never be addressed). With the refactor the username comes from
    instance.yaml ``devices.hub.keychain.account`` (or the per-kind default), NOT
    a constant pinned to the Sagemcom module.
    """
    _stub_instance(monkeypatch, {"devices.hub.keychain.account": "operator"})
    creds = net._hub_creds(_net())
    assert creds.username == "operator"


def test_hub_creds_default_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """_hub_creds with no override → the per-kind default account 'admin'."""
    _stub_instance(monkeypatch, {})
    creds = net._hub_creds(_net())
    assert creds.username == "admin"
    assert creds.host == "192.168.2.1"
    assert creds.secret is None


def test_orbi_creds_honors_account_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """_orbi_creds must route through device_creds — an account override reaches it."""
    _stub_instance(monkeypatch, {"devices.orbi.keychain.account": "netadmin"})
    creds = net._orbi_creds(_net("192.168.1.1"))
    assert creds.username == "netadmin"


def test_orbi_creds_default_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """_orbi_creds with no override → the per-kind default account 'admin'."""
    _stub_instance(monkeypatch, {})
    creds = net._orbi_creds(_net("192.168.1.1"))
    assert creds.username == "admin"
    assert creds.host == "192.168.1.1"
    assert creds.secret is None


def test_device_creds_unknown_kind_has_no_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown kind with no config → empty tuple (no silent wrong default).

    A kind with no built-in default and nothing in instance.yaml must NOT inherit
    another kind's tuple. The account falls back to empty so a misconfiguration is
    visible (the provider's Keychain read then misses loudly) rather than silently
    addressing the wrong entry.
    """
    _stub_instance(monkeypatch, {})
    service, account = net.device_keychain_ref("printer")
    assert service == ""
    assert account == ""
