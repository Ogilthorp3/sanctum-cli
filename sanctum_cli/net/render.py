from __future__ import annotations

import re
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.table import Table

if TYPE_CHECKING:
    from rich.console import Console

    from sanctum_cli.net.types import Playbook, SpeedReport, TopologyReport

_PLACEHOLDER = re.compile(r"\{(admin_url|gateway_ip|firewalla_wan_mac)\}")


def _fill(template: str, repl: dict[str, str]) -> str:
    """Single-pass placeholder substitution.

    Uses re.sub so each placeholder is replaced exactly once from `repl`;
    re.sub never rescans inserted text, so a value containing '{gateway_ip}'
    or other placeholder-looking text (or Rich markup) is inserted verbatim
    and can never trigger a second round of substitution.
    """
    return _PLACEHOLDER.sub(lambda m: repl[m.group(1)], template)


def render_plan(report: TopologyReport, playbook: Playbook) -> str:
    gw = report.gateway_ip or "your router's address"
    mac = report.firewalla_wan_mac or "your Firewalla's WAN MAC"
    admin_url = (
        _fill(
            playbook.admin_url_template,
            {"admin_url": "", "gateway_ip": gw, "firewalla_wan_mac": mac},
        )
        if playbook.admin_url_template
        else ""
    )
    repl = {"admin_url": admin_url, "gateway_ip": gw, "firewalla_wan_mac": mac}

    def sub(t: str) -> str:
        return _fill(t, repl)

    lines: list[str] = []
    lines.append(f"Network optimization plan — {playbook.display_name}")
    lines.append("")

    if playbook.achieves == "not_possible":
        for s in playbook.steps:
            lines.append(f"  • {sub(s)}")
        return "\n".join(lines)

    lines.append("⚠  This briefly drops your internet. Do this at home, at the box — not remotely.")
    lines.append("")
    if playbook.prechecks:
        lines.append("Before you start (check these FIRST):")
        for p in playbook.prechecks:
            lines.append(f"  • {sub(p)}")
        lines.append("")
    lines.append("If anything goes wrong, the ROLLBACK (undo) is:")
    for r in playbook.rollback:
        lines.append(f"    ↩ {sub(r)}")
    lines.append("")
    lines.append("Steps:")
    for i, s in enumerate(playbook.steps, start=1):
        lines.append(f"  {i}. {sub(s)}")
    if playbook.gotchas:
        lines.append("")
        lines.append("Watch out for:")
        for g in playbook.gotchas:
            lines.append(f"  • {sub(g)}")
    if playbook.ordering:
        lines.append("")
        lines.append("Then, in THIS order (this avoids the 169.254.x failure):")
        for i, o in enumerate(playbook.ordering, start=1):
            lines.append(f"  {i}. {sub(o)}")
    return "\n".join(lines)


def _gbps(value: float | None) -> str:
    return f"{value} Gbps" if value is not None else "not run"


def render_speed(console: Console, report: SpeedReport) -> None:
    """Render a SpeedReport: multi vs single side by side, the hop/ceiling
    table, the bottleneck, the verdict, and the advice bullets. All probed
    strings are escaped so network-derived text can never inject Rich markup."""
    console.print("[bold]Throughput doctor[/] — honest numbers, not the headline figure")
    console.print()

    if report.multi_gbps is None and report.single_gbps is None:
        console.print(
            "[dim]Live download not run (audit-only). "
            "Showing the path ceiling and interpretation.[/]"
        )
    else:
        measured = Table(show_header=True, header_style="bold", title="Measured (side by side)")
        measured.add_column("test")
        measured.add_column("result", justify="right")
        measured.add_row("multi-stream (real)", _gbps(report.multi_gbps))
        measured.add_row("single-stream (what most tests show)", _gbps(report.single_gbps))
        console.print(measured)
    console.print()

    hops = Table(show_header=True, header_style="bold", title="Path ceiling (slowest link wins)")
    hops.add_column("hop")
    hops.add_column("link", justify="right")
    if report.hops:
        for name, mbps in report.hops:
            link = f"{mbps} Mbps" if mbps is not None else "[dim]unknown[/]"
            hops.add_row(escape(name), link)
    else:
        hops.add_row("[dim]no link speeds detected[/]", "[dim]-[/]")
    console.print(hops)

    on_wifi = "yes" if report.on_wifi else ("no" if report.on_wifi is False else "unknown")
    console.print(f"[bold]On Wi-Fi:[/] {on_wifi}")
    console.print(f"[bold]Bottleneck:[/] {escape(report.bottleneck)}")
    console.print(f"[bold]Ceiling:[/] {_gbps(report.ceiling_gbps)}")
    if report.test_inconclusive:
        console.print(
            "[yellow]Live test was inconclusive (endpoint-limited) — number is a floor.[/]"
        )
    console.print()

    console.print(f"[bold]Verdict:[/] {escape(report.verdict)}")
    if report.advice:
        console.print()
        console.print("[bold]What this means:[/]")
        for bullet in report.advice:
            console.print(f"  - {escape(bullet)}")
