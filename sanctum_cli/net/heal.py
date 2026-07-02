"""Sanctum Net Heal — the pure half of a topology-adaptive self-healing layer.

Read a node's live L3 *posture* (interface, ConfigMethod, IP/subnet, default
gateway + reachability, association, and whether the never-strand spine — the
Tailscale tailnet and/or the TB5 bridge — is up), classify it against a pure
truth table, and plan a *guarded* heal that never strands the node, never
loops, fails closed on an unreadable posture, and stays out of the NAT domain.

This module is the additive sibling of ``sanctum_cli.net.link``: the impure
boundary is a thin injected ``CommandRunner`` (argv -> stdout), so :func:`probe_posture`
is fully unit-testable without a live network. The verdict / plan functions
(added in later tasks) are pure functions over the parsed posture.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from sanctum_cli.net.detect import parse_default_gateway
from sanctum_cli.net.link import (
    CommandRunner,
    _parse_wifi_iface,
    _real_run,
)

# ─── posture read regexes ─────────────────────────────────────────────

# ConfigMethod from `ipconfig getsummary <iface>` — "Manual" (static) vs "DHCP".
# First match wins; a node that cannot report it reads "" (fail-closed → UNVERIFIED).
_CONFIG_METHOD_RE = re.compile(r"ConfigMethod\s*:\s*(\w+)")

# LinkStatusActive : TRUE|FALSE — the live association flag (no Wi-Fi scan).
_LINK_ACTIVE_RE = re.compile(r"LinkStatusActive\s*:\s*(TRUE|FALSE)", re.IGNORECASE)

# The packet-loss percentage from a ping summary. Anchored on the "% packet loss"
# suffix so "0.0%" is NOT read out of "100.0%" — the exact false-reachable this
# guards against (a naive substring check reports total loss as reachable).
_PING_LOSS_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)%\s*packet loss")

# An IPv4 inet line from `ifconfig`, e.g. "\tinet 100.107.112.118 --> ...".
_INET_RE = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})\b")

# The Tailscale tailnet lives in the 100.64.0.0/10 CGNAT range; the TB5 bridge is
# the 10.0.5.0/24 point-to-point link (bert@10.0.5.1). Both are the never-strand
# spine: an out-of-band path that survives a LAN renumber / gateway death.
_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")
_TB5_PREFIX = "10.0.5."


@dataclass(frozen=True)
class NetPosture:
    """The node's live L3 posture — the pure input to :func:`diagnose_posture`.

    Immutable value object. ``gateway_reachable`` is ``None`` when unknown /
    unattempted (no gateway, or an unparseable ping) — never coerced to a bool,
    so a heal is never planned off an unread reachability (fail-closed). An empty
    ``iface`` / ``config_method`` means the posture could not be read → UNVERIFIED,
    never a silent healthy read. ``on_tailnet`` / ``tb5_up`` are the never-strand
    spine: mutation is only ever planned while at least one of them is alive.
    """

    iface: str
    config_method: str
    ip: str
    subnet: str
    gateway: str
    gateway_reachable: bool | None
    associated: bool
    on_tailnet: bool
    tb5_up: bool


def _first_group(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(1) if m else ""


def _spine_from_ifconfig(all_ifconfig: str) -> tuple[bool, bool]:
    """Pure: (on_tailnet, tb5_up) from the full `ifconfig` dump.

    ``on_tailnet`` is a 100.64.0.0/10 (Tailscale CGNAT) inet present on any
    interface; ``tb5_up`` is a 10.0.5.x inet (the TB5 bridge). Both are read
    from real inet lines, so a stray "100." elsewhere in the text cannot spoof
    the spine into looking alive.
    """
    on_tailnet = False
    tb5_up = False
    for m in _INET_RE.finditer(all_ifconfig):
        ip = m.group(1)
        if ip.startswith(_TB5_PREFIX):
            tb5_up = True
        try:
            if ipaddress.ip_address(ip) in _TAILNET_NET:
                on_tailnet = True
        except ValueError:
            continue
    return on_tailnet, tb5_up


def probe_posture(run: CommandRunner | None = None) -> NetPosture:
    """Read the node's live L3 posture behind an injected runner.

    Thin impure boundary — all reads via ``run`` (argv -> stdout), defaulting to
    a real subprocess seam; tests inject a fake to drive it without a network.
    Fail-closed: if the Wi-Fi interface cannot be resolved, return an all-empty /
    all-False UNVERIFIED posture (no silent ``en0`` fallback — on a Mini that is
    Ethernet, yielding a false read for the wrong link).

    Steps: resolve iface (``networksetup -listallhardwareports``); read
    ``ConfigMethod`` + ``LinkStatusActive`` from ``ipconfig getsummary``; the IP
    via ``ipconfig getifaddr``; the subnet mask via ``ipconfig getoption``; the
    default gateway via ``route -n get default`` (parsed by ``net.detect``); a
    bounded ``ping -c3 -t2 <gw>`` for reachability (``None`` when no gateway or an
    unparseable summary — never read 0.0 out of 100.0); and the never-strand
    spine (tailnet / TB5) from the full ``ifconfig`` dump.
    """
    runner = run if run is not None else _real_run
    iface = _parse_wifi_iface(runner(["networksetup", "-listallhardwareports"]))
    all_ifconfig = runner(["ifconfig"])
    on_tailnet, tb5_up = _spine_from_ifconfig(all_ifconfig)
    if not iface:
        # UNVERIFIED posture: could not identify the interface. Report the spine
        # honestly (it is read independently) but everything link-specific empty.
        return NetPosture(
            iface="",
            config_method="",
            ip="",
            subnet="",
            gateway="",
            gateway_reachable=None,
            associated=False,
            on_tailnet=on_tailnet,
            tb5_up=tb5_up,
        )

    summary = runner(["ipconfig", "getsummary", iface])
    config_method = _first_group(_CONFIG_METHOD_RE, summary)
    active = _LINK_ACTIVE_RE.search(summary)
    associated = active is not None and active.group(1).upper() == "TRUE"
    ip = runner(["ipconfig", "getifaddr", iface]).strip()
    subnet = runner(["ipconfig", "getoption", iface, "subnet_mask"]).strip()
    gateway = parse_default_gateway(runner(["route", "-n", "get", "default"])) or ""

    gateway_reachable: bool | None = None
    if gateway:
        out = runner(["ping", "-c", "3", "-t", "2", gateway])
        loss = _first_group(_PING_LOSS_RE, out)
        # Reachable only when the summary reports <100% loss; an unparseable ping
        # leaves it None (unknown) so we never claim reachable from an unread ping.
        gateway_reachable = float(loss) < 100.0 if loss else None

    return NetPosture(
        iface=iface,
        config_method=config_method,
        ip=ip,
        subnet=subnet,
        gateway=gateway,
        gateway_reachable=gateway_reachable,
        associated=associated,
        on_tailnet=on_tailnet,
        tb5_up=tb5_up,
    )
