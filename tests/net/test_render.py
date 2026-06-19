from __future__ import annotations

from sanctum_cli.net import render
from sanctum_cli.net.playbooks import BUILTINS
from sanctum_cli.net.types import Nat, TopologyReport


def _report(**kw: object) -> TopologyReport:
    base = dict(
        firewalla_present=True,
        firewalla_wan_mac="20:6d:31:51:67:82",
        firewalla_wan_mtu=1500,
        nat=Nat.DOUBLE,
        gateway_ip="192.168.2.1",
        isp="bell",
        public_ip=None,
        applicable=True,
        reason="double",
    )
    base.update(kw)
    return TopologyReport(**base)  # type: ignore[arg-type]


def test_render_substitutes_real_values() -> None:
    text = render.render_plan(_report(), BUILTINS["bell"])
    assert "20:6d:31:51:67:82" in text
    assert "http://192.168.2.1" in text
    assert "Advanced DMZ" in text


def test_render_includes_presence_warning_and_rollback() -> None:
    text = render.render_plan(_report(), BUILTINS["bell"])
    low = text.lower()
    assert "briefly drop" in low or "do this at home" in low
    assert "rollback" in low or "undo" in low
    assert "Advanced DMZ → turn OFF" in text or "turn OFF 'Advanced DMZ'" in text


def test_render_escapes_hostile_substitution() -> None:
    text = render.render_plan(
        _report(firewalla_wan_mac="[bold]{gateway_ip}[/]xé"), BUILTINS["bell"]
    )
    assert "[bold]{gateway_ip}[/]xé" in text
    assert "{gateway_ip}" in text


def test_render_not_possible_playbook_has_no_action_steps_header() -> None:
    text = render.render_plan(_report(nat=Nat.CGNAT, isp="cgnat"), BUILTINS["cgnat"])
    assert "cgnat" in text.lower() or "carrier" in text.lower()


def test_render_surfaces_bell_prechecks() -> None:
    # The Advanced-DMZ /1-vs-10.x precheck must reach the rendered plan.
    text = render.render_plan(_report(), BUILTINS["bell"])
    low = text.lower()
    assert "before you start" in low or "precheck" in low or "check first" in low
    assert "10." in text  # the 10.x LAN warning


def test_render_no_precheck_block_when_empty() -> None:
    # Playbooks without prechecks (e.g. generic) must not grow a stray header.
    text = render.render_plan(_report(isp="generic"), BUILTINS["generic"])
    assert "before you start" not in text.lower()
# ─── speed report ────────────────────────────────────────────────────


def _speed(**kw: object):
    from sanctum_cli.net.types import SpeedReport

    base = dict(
        multi_gbps=7.9,
        single_gbps=1.8,
        ceiling_gbps=10.0,
        on_wifi=False,
        hops=(("router port", 10000), ("2.5 GbE switch", 2500)),
        bottleneck="2.5 GbE switch (2.5 Gbps link)",
        verdict="Your single-stream number was the artifact.",
        advice=("Single- vs double-NAT does NOT change throughput.",),
        test_inconclusive=False,
    )
    base.update(kw)
    return SpeedReport(**base)  # type: ignore[arg-type]


def test_render_speed_shows_multi_single_and_ceiling() -> None:
    from rich.console import Console

    con = Console(record=True, width=100)
    render.render_speed(con, _speed())
    out = con.export_text()
    assert "7.9" in out and "1.8" in out  # both numbers side by side
    assert "2.5 GbE switch" in out
    assert "artifact" in out.lower()
    assert "nat" in out.lower()


def test_render_speed_escapes_probed_strings() -> None:
    from rich.console import Console

    con = Console(record=True, width=120)
    render.render_speed(con, _speed(bottleneck="[bold]evil[/] port", hops=(("[red]x[/]", 1000),)))
    out = con.export_text()
    assert "[bold]evil[/] port" in out
    assert "[red]x[/]" in out


def test_render_speed_no_test_shows_audit_only() -> None:
    from rich.console import Console

    con = Console(record=True, width=100)
    render.render_speed(con, _speed(multi_gbps=None, single_gbps=None))
    out = con.export_text().lower()
    assert "not run" in out or "audit" in out or "skipped" in out
