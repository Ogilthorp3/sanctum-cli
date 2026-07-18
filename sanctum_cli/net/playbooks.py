from __future__ import annotations

from sanctum_cli.net.types import Nat, Playbook

BUILTINS: dict[str, Playbook] = {
    "bell": Playbook(
        id="bell",
        display_name="Bell (Giga Hub / Home Hub 4000) — Advanced DMZ",
        achieves="single_nat",
        gateway_ips=("192.168.2.1",),  # ip-allow: Bell Giga Hub published default LAN gateway (playbook match data, not an endpoint)
        title_contains=("Bell", "Giga Hub", "Home Hub"),
        admin_url_template="http://{gateway_ip}",
        prechecks=(
            "Check your Firewalla LAN subnet FIRST. Bell's Advanced DMZ hands the WAN a"
            " public IP with a /1 netmask (128.0.0.0) plus a 10.x carrier gateway. A /1 is"
            " 0.0.0.0/1 - every address 0.x-127.x - so it OVERLAPS any LAN whose first octet"
            " is 1-127 (including the common 10.x). That subnet conflict breaks LAN->WAN"
            " forwarding: clients get a DHCP lease but no internet.",
            "If your LAN is on 10.x (or any 1-127.x), do ONE of these before cutover:"
            " (a) renumber the LAN to 192.168.x or 172.16-31.x (first octet >=128 is safe), OR"
            " (b) use the PPPoE-passthrough method instead (see the Bell PPPoE alternative),"
            " which keeps your 10.x LAN.",
        ),
        steps=(
            "Open {admin_url} in a browser and sign in to your Bell hub.",
            "Go to Advanced → DMZ.",
            "Add a device by MAC address: {firewalla_wan_mac}.",
            "Tick BOTH 'DMZ' and 'Advanced DMZ'. Leave any PPPoE fields BLANK.",
            "Save.",
            "After cutover, set the Firewalla WAN MTU to 1492 and confirm MSS clamping is on"
            " (Firewalla app → Network → WAN → MTU). Bell's path MTU is 1492; leaving it at"
            " 1500 silently black-holes large packets (ping works, HTTPS hangs).",
        ),
        mtu=1492,
        alt_playbook="bell-pppoe",
        gotchas=(
            "Plain DMZ alone leaves you double-NAT'd — you MUST tick 'Advanced DMZ' too.",
            "Do not enter PPPoE credentials on the Firewalla; the hub keeps the session.",
            "Advanced DMZ is INCOMPATIBLE with a 10.x LAN: it gives the WAN a /1 netmask"
            " (0.0.0.0/1 = 128.0.0.0), which overlaps any 1-127.x LAN and breaks forwarding."
            " Renumber the LAN to a first-octet->=128 subnet, or use PPPoE passthrough instead.",
            "Bell's path MTU is 1492. Set WAN MTU to 1492 (+ MSS clamp) or large packets"
            " black-hole: DF ping of a 1492-byte packet passes, a 1500-byte one fails.",
            "Advanced DMZ has NO true bridge mode on the Giga Hub 2.0 (Bell removed bridge in"
            " firmware 2.13). Your only single-NAT options are Advanced DMZ or PPPoE passthrough.",
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
        # Bell's Advanced DMZ is the method that hands the WAN a /1-poison public
        # lease — so it needs the self-healing /32 armor + the /32 poison gate.
        requires_slash32_armor=True,
    ),
    "bell-pppoe": Playbook(
        id="bell-pppoe",
        display_name="Bell (Giga Hub 2.0) — PPPoE passthrough (keeps a 10.x LAN)",
        achieves="single_nat",
        # Alternative method only — never auto-matched; reached via bell.alt_playbook.
        gateway_ips=(),
        title_contains=(),
        admin_url_template="",
        prechecks=(
            "You need your Bell PPPoE credentials (username looks like b1xxxx@bell.ca). You can"
            " set or reset the PPPoE password in MyBell if you don't have it.",
        ),
        steps=(
            "Plug the Firewalla WAN port into one of the Bell hub's LAN ports (not the WAN/ONT).",
            "In the Firewalla app, set the WAN connection type to PPPoE.",
            "Enter your Bell PPPoE credentials: username b1xxxx@bell.ca and its password"
            " (set/reset it in MyBell if needed).",
            "Set the WAN MTU to 1492 (PPPoE overhead) and leave MSS clamping on.",
            "Save. The Firewalla dials PPPoE and gets its own public IP over a point-to-point"
            " link — no /1 netmask — so your LAN can stay on 10.x.",
        ),
        mtu=1492,
        gotchas=(
            "This KEEPS your LAN on 10.x — PPPoE gives a point-to-point link (no /1 netmask),"
            " so there's no subnet conflict like Advanced DMZ has. IPv6 also works.",
            "Single-threaded PPPoE can cap throughput above ~1.5 Gbps. For the fastest raw"
            " speed prefer Advanced DMZ (but that needs a non-10.x LAN). A Firewalla Gold Pro"
            " handles line-rate PPPoE on typical plans.",
            "The PPPoE credentials are human-held (b1xxxx@bell.ca) — keep them somewhere safe;"
            " this tool never stores them.",
        ),
        ordering=(
            "Move the Firewalla WAN cable to a hub LAN port FIRST, then configure PPPoE.",
            "After saving PPPoE, renew/bounce the Firewalla WAN if it doesn't dial immediately.",
        ),
        rollback=(
            "In the Firewalla app, set the WAN connection type back to DHCP.",
            "Move the Firewalla WAN cable back to its original port if you changed it.",
            "Confirm the Firewalla WAN returns to a private address (working double-NAT).",
        ),
        requires_slash32_armor=True,
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
        # A generic ISP's passthrough yields a normal public lease — no /1 poison, so
        # the /32 armor stages are skipped and any public prefix commits.
        requires_slash32_armor=False,
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
        # CGNAT single-NAT is not reachable from our side, so there is no cutover +
        # no /32 armor.
        requires_slash32_armor=False,
    ),
}


def match(*, gateway_ip: str | None, http_title: str, nat: Nat) -> Playbook:
    if nat is Nat.CGNAT:
        return BUILTINS["cgnat"]
    title = (http_title or "").lower()
    for pb in BUILTINS.values():
        # "bell-pppoe" is an alternative method reached via bell.alt_playbook,
        # never auto-matched (same exclusion as the non-ISP-specific entries).
        if pb.id in {"generic", "cgnat", "bell-pppoe"}:
            continue
        if gateway_ip and gateway_ip in pb.gateway_ips:
            return pb
        if any(tok.lower() in title for tok in pb.title_contains):
            return pb
    return BUILTINS["generic"]
