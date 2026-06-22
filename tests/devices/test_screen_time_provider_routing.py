"""Pin that ``screen_time`` reads the Firewalla bridge through the provider.

Task-3 contract: ``sanctum_cli.commands.screen_time`` must consume
:class:`sanctum_cli.devices.firewalla.FirewallaProvider` for its bridge access
instead of its own private httpx plumbing. These tests assert the *routing* —
that screen_time's ``_fetch_bridge_json`` delegates to the provider's
module-level bridge-read seam — WITHOUT changing the public, fail-soft
``dict | None`` behavior the characterization suite already pins.

SAFETY: the provider seam is monkeypatched in every test; nothing here touches
the live Firewalla or opens a socket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sanctum_cli.commands import screen_time as st
from sanctum_cli.devices import firewalla as fw

if TYPE_CHECKING:
    import pytest


def test_screen_time_read_routes_through_provider_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A screen_time bridge read must go through the FirewallaProvider seam.

    When the provider's module-level ``_fetch_bridge_json`` is monkeypatched,
    screen_time's ``_fetch_bridge_json`` must observe that patched seam — proof
    the read is routed through the provider, not a private httpx call screen_time
    still owns.
    """
    seen: list[str] = []

    def fake_provider_fetch(path: str, **_kwargs: Any) -> dict[str, Any] | None:
        seen.append(path)
        return {"routed": True, "path": path}

    monkeypatch.setattr(fw, "_fetch_bridge_json", fake_provider_fetch)
    # A token must resolve for the read to reach the provider seam.
    monkeypatch.setenv(st._BRIDGE_TOKEN_ENV, "tok-from-env")

    out = st._fetch_bridge_json("/info")

    assert out == {"routed": True, "path": "/info"}
    assert seen == ["/info"]


def test_screen_time_read_fail_soft_routes_none_from_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``None`` from the provider seam fails soft through screen_time unchanged."""
    monkeypatch.setattr(fw, "_fetch_bridge_json", lambda *_a, **_k: None)
    monkeypatch.setenv(st._BRIDGE_TOKEN_ENV, "tok-from-env")
    assert st._fetch_bridge_json("/policies") is None
