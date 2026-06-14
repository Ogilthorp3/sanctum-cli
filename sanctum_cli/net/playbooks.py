from __future__ import annotations

from sanctum_cli.net.types import Nat, Playbook

BUILTINS: dict[str, Playbook] = {
    "bell": Playbook(
        id="bell",
        display_name="Bell (Giga Hub / Home Hub 4000)",
        achieves="single_nat",
        gateway_ips=("192.168.2.1",),
        title_contains=("Bell", "Giga Hub", "Home Hub"),
        admin_url_template="http://{gateway_ip}",
        steps=(
            "Open {admin_url} in a browser and sign in to your Bell hub.",
            "Go to Advanced → DMZ.",
            "Add a device by MAC address: {firewalla_wan_mac}.",
            "Tick BOTH 'DMZ' and 'Advanced DMZ'. Leave any PPPoE fields BLANK.",
            "Save.",
        ),
        gotchas=(
            "Plain DMZ alone leaves you double-NAT'd — you MUST tick 'Advanced DMZ' too.",
            "Do not enter PPPoE credentials on the Firewalla; the hub keeps the session.",
        ),
        ordering=(
            "Power-cycle the Bell hub: unplug 1–2 minutes, wait for solid lights.",  # noqa: RUF001
            "Only AFTER the hub is fully up, renew the Firewalla WAN (app → WAN → Save, or"
            " bounce the WAN cable ~20s).",
        ),
        rollback=(
            "In the hub: Advanced → DMZ → turn OFF 'Advanced DMZ'.",
            "Renew the Firewalla WAN.",
            "Confirm the Firewalla WAN returns to a 192.168.x.x address (back to working double-NAT).",
        ),
    ),
    "generic": Playbook(
        id="generic",
        display_name="Generic ISP router",
        achieves="single_nat",
        gateway_ips=(),
        title_contains=(),
        admin_url_template="http://{gateway_ip}",
        steps=(
            "Open {admin_url} and sign in to your router/gateway.",
            "Find the 'DMZ', 'Exposed Host', or 'IP Passthrough' setting (often under"
            " Advanced or Firewall).",
            "Point it at your Firewalla's WAN MAC or IP: {firewalla_wan_mac}.",
            "Save / apply.",
        ),
        gotchas=(
            "Some routers call this 'IP Passthrough' or 'Bridge a single device' — same idea.",
        ),
        ordering=(
            "Power-cycle the gateway, wait for it to fully come up.",
            "Then renew the Firewalla WAN (or bounce its WAN cable ~20s).",
        ),
        rollback=(
            "Turn OFF the DMZ / Exposed Host / IP Passthrough setting.",
            "Renew the Firewalla WAN; confirm it returns to a private address (working double-NAT).",
        ),
    ),
    "cgnat": Playbook(
        id="cgnat",
        display_name="Carrier-Grade NAT (not optimizable)",
        achieves="not_possible",
        gateway_ips=(),
        title_contains=(),
        admin_url_template="",
        steps=(
            "Your ISP places you behind CGNAT, so single-NAT can't be reached from your side.",
            "If you need it, ask your ISP for a 'public/static IP' or to disable CGNAT.",
        ),
        gotchas=("This is an ISP-side setting; no change on your equipment will fix it.",),
        ordering=(),
        rollback=(),
    ),
}


def match(*, gateway_ip: str | None, http_title: str, nat: Nat) -> Playbook:
    if nat is Nat.CGNAT:
        return BUILTINS["cgnat"]
    title = (http_title or "").lower()
    for pb in BUILTINS.values():
        if pb.id in {"generic", "cgnat"}:
            continue
        if gateway_ip and gateway_ip in pb.gateway_ips:
            return pb
        if any(tok.lower() in title for tok in pb.title_contains):
            return pb
    return BUILTINS["generic"]
