from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sanctum_cli.net.types import Playbook, TopologyReport

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
        _fill(playbook.admin_url_template, {"admin_url": "", "gateway_ip": gw, "firewalla_wan_mac": mac})
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
