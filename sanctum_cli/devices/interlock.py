"""Tailscale-on-box out-of-band probe — the LAN-independent recovery channel.

The single-NAT DMZ cutover drops the WAN, and on 2026-06-26 every recovery
channel it trusted was LAN-bound (the Mini jump host + the Firewalla over the
LAN), so when Bell's poison ``/1`` collapsed the ``10.x`` LAN the "proven"
recovery path went dark with it. The fix is a recovery channel that does NOT ride
the LAN being changed: **Tailscale-on-box** — node ``ts-firewalla``
(:data:`TS_FIREWALLA_ADDR`), root SSH over the tailnet, proven LAN-independent.

This module is the *boundary* the prevent-interlock's "OOB channel proven-live"
precondition (see :func:`sanctum_cli.devices.flip.evaluate_interlock`) is derived
from. The honest proof is a real root-SSH **round-trip** — ``ssh root@<addr>
true`` returning exit 0 — NOT a bare ping/TCP-connect (which proves only L3
reachability, not a usable recovery channel; CLAUDE.md "Contracts at the
Boundary"). The transport is injectable (``probe=``) so the gate is fully
testable offline, with the real subprocess SSH the default.

Fail-OPEN is the footgun this module is built to avoid: the probe NEVER returns 0
(live) on a spawn error / timeout / any non-zero ssh exit — a swallowed failure
that read as "live" would re-create the 06-26 blindness. ``_run_probe`` collapses
every failure into a non-zero sentinel, so :func:`tailscale_oob_live` reads it as
not-live.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

# The Tailscale node for the Firewalla (``ts-firewalla``, tag:sanctum-host). Root
# SSH over the tailnet to this address is the LAN-INDEPENDENT recovery channel —
# proven to survive a LAN collapse (the Mini root-SSH'd it over Tailscale while on
# a different network). This is the channel the interlock's OOB precondition probes.
TS_FIREWALLA_ADDR = "100.68.36.16"

# Bounded so a dead tailnet fails FAST (fail-closed) rather than hanging the 2 a.m.
# cutover: the ssh connect timeout, and a hard wall on the whole round-trip.
_SSH_CONNECT_TIMEOUT = 5
_PROBE_TIMEOUT = 12

# A tailnet probe: maps an address to the probe's exit code (0 == channel live).
# The injectable seam tests record over; the real default is _real_tailnet_ssh_probe.
TailnetProbe = Callable[[str], int]


def _ts_ssh_argv(addr: str, remote: str) -> list[str]:
    """Build the key-only root-SSH-over-tailnet argv (one element per token).

    Mirrors the Firewalla SSH envelope (:func:`sanctum_cli.net.system._fw_ssh_argv`):
    key-only / publickey-only / host-key accept-new / bounded connect timeout — but
    as ``root@<tailnet-addr>``. Built as a LIST (never a shell string), exactly one
    element per token, so a hostile ``addr``/``remote`` carrying a space / ``%`` /
    non-ASCII char can never inject an argument (CLAUDE.md: own the boundary).
    """
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={_SSH_CONNECT_TIMEOUT}",
        f"root@{addr}",
        remote,
    ]


def _run_probe(argv: list[str]) -> int:
    """Run one probe argv, returning its exit code; a NON-zero sentinel on any failure.

    NEVER raises and NEVER returns 0 on a spawn error / timeout — a probe that could
    not run is the ABSENCE of proof the channel is live, so it must read as not-live
    (fail-closed). 124 is the conventional timeout sentinel; OSError/ValueError
    (spawn failures) also map to it.
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 124
    return proc.returncode


def _real_tailnet_ssh_probe(addr: str) -> int:
    """The real OOB proof: a root-SSH round-trip over the tailnet (``ssh root@addr true``).

    Exit 0 means the channel is genuinely usable (auth succeeded + a command ran),
    not merely that the address pings — the honest-verify contract.
    """
    return _run_probe(_ts_ssh_argv(addr, "true"))


def tailscale_oob_live(
    *, addr: str = TS_FIREWALLA_ADDR, probe: TailnetProbe | None = None
) -> bool:
    """Is the LAN-independent Tailscale-on-box OOB channel live RIGHT NOW?

    The canonical proof is a root-SSH round-trip to ``addr`` over the tailnet
    returning exit 0. ``probe`` is injectable (tests pass a recording double mapping
    an address to a scripted exit code) so this gate is exercised offline; the real
    default is :func:`_real_tailnet_ssh_probe`. Returns True ONLY on exit 0 — any
    non-zero / timeout / spawn-failure reads as not-live (fail-closed).
    """
    return (probe or _real_tailnet_ssh_probe)(addr) == 0
