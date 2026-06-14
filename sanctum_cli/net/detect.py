from __future__ import annotations

import ipaddress
import re

from sanctum_cli.net.types import Nat

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
    if hop2 and not _is_private(hop2):
        return Nat.SINGLE
    if hop2 and _is_private(hop2):
        return Nat.DOUBLE
    if wan_ip and _is_private(wan_ip):
        return Nat.DOUBLE
    if wan_ip and not _is_private(wan_ip):
        return Nat.SINGLE
    return Nat.UNKNOWN


def parse_default_gateway(route_output: str) -> str | None:
    """Parse `route -n get default` output for the gateway IP."""
    for line in route_output.splitlines():
        if "gateway:" in line:
            m = _IPV4.search(line)
            return m.group(1) if m else None
    return None


def parse_mtu(ifconfig_output: str) -> int | None:
    m = re.search(r"\bmtu (\d+)\b", ifconfig_output)
    return int(m.group(1)) if m else None
