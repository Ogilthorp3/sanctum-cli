"""Pure tests for sanctum net home product roll-up."""

from __future__ import annotations

from sanctum_cli.net.home import (
    ArmorState,
    Health,
    HubReach,
    InternetPath,
    MssGuard,
    Overall,
    WanPath,
    build_home_report,
)


def _ok_inputs(**overrides: object):
    base = {
        "internet": InternetPath(True, True, "Fastly+Google TLS OK"),
        "wan": WanPath("pppoe", "1.2.3.4", "pppoe0", "PPPoE"),
        "mss": MssGuard(True, True, "MSS 1400"),
        "armor": ArmorState("HEALTHY", True, False, "HEALTHY"),
        "hub": HubReach(False, "192.168.2.1", "unreachable"),
    }
    base.update(overrides)
    return base


def test_green_when_all_ok() -> None:
    r = build_home_report(**_ok_inputs())
    assert r.overall is Overall.GREEN
    assert r.improve_safe is False


def test_degraded_when_cdn_broken() -> None:
    r = build_home_report(
        **_ok_inputs(
            internet=InternetPath(False, True, "Fastly fail Google ok"),
            mss=MssGuard(False, False, "no clamp"),
        )
    )
    assert r.overall is Overall.DEGRADED
    assert r.improve_safe is True
    assert any(row.health is Health.DOWN for row in r.rows)


def test_hub_unreachable_is_ok_not_degraded() -> None:
    r = build_home_report(**_ok_inputs(hub=HubReach(False, "192.168.2.1", "no hub")))
    assert r.overall is Overall.GREEN
    hub_row = next(x for x in r.rows if x.label.startswith("Hub"))
    assert hub_row.health is Health.OK


def test_hub_reachable_is_attention() -> None:
    r = build_home_report(**_ok_inputs(hub=HubReach(True, "192.168.2.1", "up")))
    assert r.overall is Overall.ATTENTION
    hub_row = next(x for x in r.rows if x.label.startswith("Hub"))
    assert hub_row.health is Health.ATTENTION


def test_unknown_probes_fail_open() -> None:
    r = build_home_report(internet=None, wan=None, mss=None, armor=None, hub=None)
    assert r.overall is Overall.GREEN  # all UNKNOWN → fail-open
    assert all(row.health is Health.UNKNOWN for row in r.rows)


def test_private_wan_attention() -> None:
    r = build_home_report(**_ok_inputs(wan=WanPath("private", None, "eth0", "192.168.2.x")))
    assert r.overall is Overall.ATTENTION
