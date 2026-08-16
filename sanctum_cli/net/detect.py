from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable

from sanctum_cli.net import playbooks
from sanctum_cli.net.system import parse_default_iface
from sanctum_cli.net.types import Nat, TopologyReport

Runner = Callable[[tuple[str, ...]], str]
HttpProbe = Callable[[str], tuple[int, str]]

_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def is_apipa(ip: str | None) -> bool:
    if not ip:
        return False
    return ip.startswith("169.254.")


def _is_cgnat(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


_BELL_DMZ_WAN_NET = ipaddress.ip_network("0.0.0.0/1")  # 0.x-127.x


def lan_conflicts_with_bell_dmz(lan_cidr: str) -> bool:
    """True iff a LAN would overlap Bell's Advanced-DMZ WAN network.

    Bell's Advanced DMZ passthrough hands the Firewalla WAN a public IP with a
    /1 netmask (128.0.0.0) - i.e. 0.0.0.0/1, covering every address 0.x-127.x.
    Any LAN whose network falls (even partly) inside that range conflicts with
    the WAN, and the Firewalla refuses to forward LAN->WAN (clients lease, no
    internet). Common 10.x and 172.x-as-1-127 LANs trip this; 192.168.x and
    172.16-31.x (first octet >=128 is always safe) do not.

    Bad/unparseable input returns False (no false alarm).
    """
    try:
        net = ipaddress.ip_network(lan_cidr, strict=False)
    except ValueError:
        return False
    return net.overlaps(_BELL_DMZ_WAN_NET)


def parse_hop2(traceroute_output: str) -> str | None:
    """Return the first IPv4 on the hop-2 line of `traceroute -n` output, else None."""
    for line in traceroute_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("2 ") or stripped.startswith("2\t"):
            m = _IPV4.search(stripped[1:])
            return m.group(1) if m else None
    return None


def classify_nat(*, hop2: str | None, wan_ip: str | None) -> Nat:
    """Single vs double vs CGNAT, mirroring net-audit.sh's logic."""
    if _is_cgnat(wan_ip) or _is_cgnat(hop2):
        return Nat.CGNAT
    if wan_ip:
        if _is_private(wan_ip):
            return Nat.DOUBLE
        return Nat.SINGLE
    if hop2:
        if _is_private(hop2):
            return Nat.DOUBLE
        return Nat.SINGLE
    return Nat.UNKNOWN


def parse_default_gateway(route_output: str) -> str | None:
    """Parse `route -n get default` output for the gateway IP."""
    for line in route_output.splitlines():
        if "gateway:" in line:
            m = _IPV4.search(line)
            return m.group(1) if m else None
    return None


def parse_mtu(ifconfig_output: str, iface: str | None = None) -> int | None:
    if iface:
        # Locate the block for this interface specifically
        pattern = rf"(?:^|\n){re.escape(iface)}:?\s+(.*?)(?:\n\S|\Z)"
        match = re.search(pattern, ifconfig_output, re.DOTALL)
        if match:
            ifconfig_output = match.group(1)
        else:
            return None
    m = re.search(r"\bmtu (\d+)\b", ifconfig_output)
    return int(m.group(1)) if m else None


def detect(*, runner: Runner, http: HttpProbe, firewalla_present: bool) -> TopologyReport:
    hop2 = parse_hop2(runner(("traceroute",)))
    wan_ip = runner(("fw_wan_ip",)).strip() or None
    wan_mac = runner(("fw_wan_mac",)).strip() or None
    route_out = runner(("route",))
    iface = parse_default_iface(route_out)
    mtu = parse_mtu(runner(("ifconfig",)), iface)
    public_ip = runner(("public_ip",)).strip() or None
    nat = classify_nat(hop2=hop2, wan_ip=wan_ip)
    gateway_ip = hop2 if (hop2 and _is_private(hop2)) else None

    _, title = http(f"http://{gateway_ip}") if gateway_ip else (0, "")
    pb = playbooks.match(gateway_ip=gateway_ip, http_title=title, nat=nat)

    applicable, reason = _decide(firewalla_present=firewalla_present, nat=nat)
    return TopologyReport(
        firewalla_present=firewalla_present,
        firewalla_wan_mac=wan_mac,
        firewalla_wan_mtu=mtu,
        nat=nat,
        gateway_ip=gateway_ip,
        isp=pb.id,
        public_ip=public_ip,
        applicable=applicable,
        reason=reason,
        wan_ip=wan_ip,
    )


def _decide(*, firewalla_present: bool, nat: Nat) -> tuple[bool, str]:
    if not firewalla_present:
        return False, "No Firewalla detected — nothing to optimize."
    if nat is Nat.SINGLE:
        return False, "Already single-NAT — your network is optimal."
    if nat is Nat.CGNAT:
        return False, "Your ISP uses CGNAT (carrier-grade NAT); single-NAT isn't achievable here."
    if nat is Nat.UNKNOWN:
        return False, "Could not determine NAT topology — skipping (no change made)."
    return True, "Double-NAT detected behind your ISP gateway — optimization available."
