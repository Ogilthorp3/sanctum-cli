from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sanctum_cli.net.types import Playbook, TopologyReport


def _subst(template: str, *, gateway_ip: str, admin_url: str, mac: str) -> str:
    """Single-pass substitution of ONLY our known keys.

    We do NOT use str.format()/Template so that a hostile value containing
    '{gateway_ip}' or Rich markup is inserted verbatim and never re-expanded.
    """
    out = template
    out = out.replace("{admin_url}", admin_url)
    out = out.replace("{gateway_ip}", gateway_ip)
    out = out.replace("{firewalla_wan_mac}", mac)
    return out


def render_plan(report: TopologyReport, playbook: Playbook) -> str:
    gw = report.gateway_ip or "your router's address"
    admin_url = (
        playbook.admin_url_template.replace("{gateway_ip}", gw)
        if playbook.admin_url_template
        else ""
    )
    mac = report.firewalla_wan_mac or "your Firewalla's WAN MAC"

    def sub(t: str) -> str:
        return _subst(t, gateway_ip=gw, admin_url=admin_url, mac=mac)

    lines: list[str] = []
    lines.append(f"Network optimization plan — {playbook.display_name}")
    lines.append("")

    if playbook.achieves == "not_possible":
        for s in playbook.steps:
            lines.append(f"  • {sub(s)}")
        return "\n".join(lines)

    lines.append("⚠  This briefly drops your internet. Do this at home, at the box — not remotely.")
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
