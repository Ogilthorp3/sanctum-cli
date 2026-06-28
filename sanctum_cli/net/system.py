from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sanctum_cli.net.detect import Runner

# Tag -> argv for host-side probes that exist on any Mac. The Firewalla WAN
# tags ("fw_wan_ip", "fw_wan_mac") are resolved by make_real_runner below,
# which composes one cached Firewalla SSH probe over this real_runner. The bare
# real_runner still returns "" for the fw tags (callers handle that path).
_COMMANDS: dict[tuple[str, ...], list[str]] = {
    ("traceroute",): ["traceroute", "-n", "-w", "2", "-q", "3", "-m", "2", "1.1.1.1"],
    ("route",): ["route", "-n", "get", "default"],
    ("ifconfig",): ["ifconfig"],
    ("airport_ports",): ["networksetup", "-listallhardwareports"],
}

# Every tag :func:`real_runner` actually resolves: the host-side command probes
# above plus the two special-cased probes. The hard-fail-on-unknown path in
# :func:`make_real_runner` consults this so a *known* host probe still delegates
# while an unknown apply-path tag raises instead of silently no-op'ing.
_REAL_RUNNER_TAGS: frozenset[tuple[str, ...]] = frozenset(_COMMANDS) | {
    ("public_ip",),
    ("link_speed",),
}


def real_runner(tag: tuple[str, ...]) -> str:
    if tag == ("public_ip",):
        return _http_text("https://api.ipify.org")
    if tag == ("link_speed",):
        return _link_speed_text()
    argv = _COMMANDS.get(tag)
    if argv is None:
        return ""
    return _run(argv)


def _run(argv: list[str], timeout: int = 20) -> str:
    """Run a command, returning stdout; never raise (empty on any failure)."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return ""
    return proc.stdout


def _link_speed_text() -> str:
    """Raw media/link-rate text for the default interface (macOS, then Linux).

    Resolves the default iface from `route`, then asks `ifconfig <if>` (macOS
    prints a `media:` line with the PHY rate). Falls back to `ethtool <if>` on
    Linux. Returns "" on any failure — the parser handles empty.
    """
    iface = parse_default_iface(_run(["route", "-n", "get", "default"]))
    if not iface:
        return ""
    out = _run(["ifconfig", iface])
    if "media:" in out or "media " in out:
        return out
    ethtool = _run(["ethtool", iface])
    return ethtool or out


def _http_text(url: str, timeout: int = 8) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw: bytes = r.read(256)
            return raw.decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def real_http(url: str, timeout: int = 5) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(4096).decode("utf-8", "replace")
            return (getattr(r, "status", 200), _title(body))
    except (urllib.error.URLError, OSError, ValueError):
        return (0, "")


def _title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _fw_ssh_argv(gateway: str, key: str, remote: str, *, user: str = "pi") -> list[str]:
    """Build the key-only Firewalla SSH argv for ``remote`` (the one SSH envelope).

    The single source of the SSH-options shape every Firewalla command shares —
    key-only (no password prompt hang), publickey-only, host-key accept-new, a
    bounded connect timeout. Read AND mutating callers compose their remote
    command string and route it through here so the transport envelope never
    drifts between probe and cutover.
    """
    return [
        "ssh",
        "-i",
        key,
        "-o",
        "BatchMode=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=5",
        f"{user}@{gateway}",
        remote,
    ]


def firewalla_box_preflight(gateway: str, key: str, user: str = "pi") -> tuple[bool, bool]:
    """SSH the box (key-only, read-only) and probe (passwordless_sudo, dhclient_present).

    The pre-apply box gate's I/O boundary (FIX-f). Runs over the SAME key-SSH envelope
    (:func:`_fw_ssh_argv`) the cutover's box ops use, so it checks the exact transport
    the ``sudo dhclient`` re-lease will run on. The remote prints a distinct marker for
    each capability that holds:

    * ``sudo -n true`` succeeds → ``SUDO_OK`` (passwordless sudo is configured), and
    * ``command -v dhclient`` resolves → ``DHCLIENT_OK`` (a real DHCP client exists).

    Returns ``(passwordless_sudo, dhclient_present)`` parsed from those markers.
    Fail-closed: ANY transport failure (cannot spawn / timeout) returns
    ``(False, False)`` — the absence of proof is treated as not-ready, so a box we
    could not reach refuses the cutover rather than green-lighting it. The whole probe
    is one ``;``-joined remote so a failing ``sudo`` (or absent ``dhclient``) never
    short-circuits the other check.
    """
    remote = (
        "sudo -n true 2>/dev/null && echo SUDO_OK; "
        "command -v dhclient >/dev/null 2>&1 && echo DHCLIENT_OK"
    )
    argv = _fw_ssh_argv(gateway, key, remote, user=user)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=12, check=False
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return (False, False)
    out = proc.stdout
    return ("SUDO_OK" in out, "DHCLIENT_OK" in out)


def firewalla_wan_via_ssh(gateway: str, key: str, user: str = "pi") -> tuple[str, str]:
    """SSH to the Firewalla (key-only, read-only) and return (wan_ip, wan_mac).

    Returns ("", "") on any failure. Reads the WAN interface's MAC + IPv4.
    """
    remote = (
        "D=$(ip -o route get 1.1.1.1 2>/dev/null | grep -oE 'dev [a-z0-9.]+' | cut -d' ' -f2); "
        "cat /sys/class/net/$D/address 2>/dev/null; "
        "ip -o -4 addr show $D 2>/dev/null | awk '{print $4}' | cut -d/ -f1"
    )
    argv = _fw_ssh_argv(gateway, key, remote, user=user)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=12, check=False
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return ("", "")
    mac, ip = "", ""
    for line in proc.stdout.splitlines():
        s = line.strip()
        if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", s):
            mac = s
        elif re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", s):
            ip = s
    return (ip, mac)


# ─── speedtest probe parsers (pure) ──────────────────────────────────

_MEDIA_RATE = re.compile(r"(\d+(?:\.\d+)?)\s*g?base", re.IGNORECASE)
_MEDIA_GBIT = re.compile(r"\b(\d+(?:\.\d+)?)\s*g(?:base|bit|b)?", re.IGNORECASE)
_ETHTOOL_SPEED = re.compile(r"Speed:\s*(\d+)\s*Mb/s", re.IGNORECASE)


def parse_default_iface(route_output: str) -> str | None:
    """Parse `route -n get default` for the egress interface name (e.g. en7)."""
    for line in route_output.splitlines():
        if "interface:" in line:
            return line.split("interface:", 1)[1].strip() or None
    return None


def parse_link_speed_mbps(text: str) -> int | None:
    """Link/PHY speed in Mbps from `ifconfig` media line or `ethtool` output.

    Handles macOS media strings ('10Gbase-T', '2500Base-T', '1000baseT') and
    Linux ethtool ('Speed: 2500Mb/s'). Returns None when no rate is present.
    """
    m = _ETHTOOL_SPEED.search(text)
    if m:
        return int(m.group(1))
    # macOS: prefer the explicit base-rate token (e.g. 2500Base-T, 1000baseT).
    for line in text.splitlines():
        if "base" not in line.lower():
            continue
        rate = _MEDIA_RATE.search(line)
        if not rate:
            continue
        value = float(rate.group(1))
        # "10Gbase" -> the G means gigabit; "1000baseT" -> already Mbps.
        seg = line[rate.start() : rate.end()].lower()
        if "gbase" in seg or seg.endswith("g"):
            return int(value * 1000)
        gbit = _MEDIA_GBIT.search(line)
        if gbit and value < 100:  # e.g. "10 Gbase" split oddly
            return int(float(gbit.group(1)) * 1000)
        return int(value)
    return None


def iface_is_wifi(iface: str, hardware_ports: str) -> bool | None:
    """True if `iface` is the Wi-Fi port per `networksetup -listallhardwareports`.

    Returns None when the interface is not found in the listing (unknown).
    """
    port_name: str | None = None
    for line in hardware_ports.splitlines():
        s = line.strip()
        if s.startswith("Hardware Port:"):
            port_name = s.split(":", 1)[1].strip()
        elif s.startswith("Device:"):
            dev = s.split(":", 1)[1].strip()
            if dev == iface:
                return port_name is not None and "wi-fi" in port_name.lower()
    return None


def parse_speedtest_cli_mbps(blob: str) -> float | None:
    """Download Mbps from Ookla `speedtest --format=json` output.

    Ookla reports download.bandwidth in BYTES/sec; *8/1e6 -> Mbps.
    Returns None on malformed JSON or a missing/zero field.
    """
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return None
    try:
        bw = data["download"]["bandwidth"]
    except (KeyError, TypeError):
        return None
    if not isinstance(bw, (int, float)) or bw <= 0:
        return None
    return round(bw * 8 / 1_000_000, 1)


# ─── live throughput probe (bounded, never hangs) ────────────────────

# Cloudflare 403s without a browser UA; set one. Bounded byte count per stream.
_DOWN_URL = "https://speed.cloudflare.com/__down?bytes={n}"
_DOWN_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) sanctum-cli/net-speedtest"
_STREAM_BYTES = 50_000_000  # 50 MB per stream — bounded, fast on a multi-gig line
_STREAM_TIMEOUT = 12  # hard per-request cap (seconds)


def _ookla_download_mbps() -> float | None:
    """Download Mbps via the Ookla `speedtest` CLI if installed, else None."""
    out = _run(["speedtest", "--accept-license", "--accept-gdpr", "--format=json"], timeout=90)
    return parse_speedtest_cli_mbps(out) if out else None


def _one_stream_mbps(url: str, deadline: float) -> float | None:
    """Download one bounded stream; return its Mbps, or None on failure/timeout."""
    budget = deadline - time.monotonic()
    if budget <= 0:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": _DOWN_UA})
    try:
        start = time.monotonic()
        total = 0
        with urllib.request.urlopen(req, timeout=min(_STREAM_TIMEOUT, budget)) as r:
            while True:
                if time.monotonic() >= deadline:
                    break
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
        elapsed = time.monotonic() - start
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if elapsed <= 0 or total <= 0:
        return None
    return round(total * 8 / 1_000_000 / elapsed, 1)


def live_throughput(
    streams: int = 8, *, duration: float = 10.0
) -> tuple[float | None, float | None, bool]:
    """Return (multi_gbps, single_gbps, inconclusive).

    Prefers the Ookla `speedtest` CLI (single authoritative number for both).
    Otherwise runs N bounded parallel HTTPS streams (summed) AND one lone
    stream for contrast. If too few streams succeed (rate-limited / endpoint-
    limited), inconclusive=True and the numbers are reported as a FLOOR.

    Every request is time-bounded; this never hangs and never raises.
    """
    ookla = _ookla_download_mbps()
    if ookla is not None:
        gbps = round(ookla / 1000, 3)
        # Ookla is already multi-connection; no separate single-stream contrast.
        return gbps, None, False

    streams = max(1, streams)
    url = _DOWN_URL.format(n=_STREAM_BYTES)

    # Multi-stream: N parallel, summed throughput within the duration budget.
    multi_deadline = time.monotonic() + duration
    results: list[float | None] = []
    try:
        with ThreadPoolExecutor(max_workers=streams) as pool:
            futures = [pool.submit(_one_stream_mbps, url, multi_deadline) for _ in range(streams)]
            results = [f.result() for f in futures]
    except (RuntimeError, OSError):
        results = []
    ok = [m for m in results if m is not None]
    multi_mbps = sum(ok) if ok else None

    # Single-stream contrast (one connection), shorter budget.
    single_mbps = _one_stream_mbps(url, time.monotonic() + min(duration, 6.0))

    inconclusive = len(ok) < max(1, streams // 2)  # fewer than half completed
    multi_gbps = round(multi_mbps / 1000, 3) if multi_mbps is not None else None
    single_gbps = round(single_mbps / 1000, 3) if single_mbps is not None else None
    if multi_gbps is None and single_gbps is None:
        inconclusive = True
    return multi_gbps, single_gbps, inconclusive


# ─── single-NAT (Bell Advanced DMZ) mutating Firewalla/firerouter ops ─────────
#
# The four mutating tags the single-NAT DMZ orchestrator fires (mirrors
# sanctum_cli.devices.intents._RUNNER_WAN_DHCP / _LEASE_OBSERVE / _ARMOR_ARM /
# _DHCP_RELEASE). Each maps to a real firerouter command run on the Firewalla
# over the fw key. The command strings are authored from the kit's own watchdog
# (sanctum-singlenat-armor/bin/singlenat-watchdog.sh + singlenat-verify.sh) so
# the CLI's runner and the armor's self-heal issue the SAME firerouter ops:
#
#   * wan_dhcp     — switch the WAN to DHCP/PPPoE passthrough: derive the WAN dev
#                    from the default route, release + re-acquire a DHCP lease on
#                    it (the watchdog's fallback_double_nat dhclient pattern,
#                    WITHOUT the hook removal — this engages the passthrough, it
#                    does not revert single-NAT).
#   * lease_observe— READ the downstream WAN's primary IPv4 and RETURN it (the
#                    verify.sh WANIP capture) so flip.should_retry_apipa can
#                    classify the lease. The ONE read among the four.
#   * dhcp_release — re-lease the WAN (release + re-acquire) so the downstream
#                    router pulls a fresh lease (the rollback re-lease).
#   * armor_arm    — arm the boot-armor persistence: re-run post_main.sh, which
#                    (re)installs the self-asserting /32 DHCP hook + MTU clamp
#                    (the README "run once" step). The launchd bootstrap of the
#                    Mini watchdog/sentinel is the armor INSTALLER's job (FIX-6 /
#                    SinglenatArmorInstaller); this tag arms the box-side hook.
#
# The WAN-dev derivation is shared (the default route's dev, with a pppoe0/eth0
# fallback) so every op targets the same interface the box actually routes over.
_FW_WAN_DEV = (
    'WAN=$(ip route show default 2>/dev/null | grep -m1 -oE "dev [a-z0-9.]+" | awk "{print \\$2}"); '
    '[ -z "$WAN" ] && for c in pppoe0 eth0; do [ -e "/sys/class/net/$c" ] && { WAN="$c"; break; }; done'
)
# release + re-acquire a DHCP lease on the derived WAN dev (shared by wan_dhcp +
# dhcp_release — both want the downstream router to pull a fresh DHCP lease).
_FW_RELEASE_RENEW = 'sudo dhclient -r "$WAN" 2>/dev/null; sudo dhclient "$WAN" 2>/dev/null'

_FW_MUTATING_REMOTE: dict[tuple[str, ...], str] = {
    ("wan_dhcp",): f"{_FW_WAN_DEV}; {_FW_RELEASE_RENEW}",
    # Pure READ of the WAN's primary IPv4 — never mutates the lease.
    ("lease_observe",): (
        f"{_FW_WAN_DEV}; "
        'ip -4 -o addr show dev "$WAN" 2>/dev/null | grep -m1 -oE "inet [0-9.]+" | awk "{print \\$2}"'
    ),
    ("dhcp_release",): f"{_FW_WAN_DEV}; {_FW_RELEASE_RENEW}",
    # Re-run the boot-armor (post_main.sh) to (re)install the persistence hook + MTU.
    ("armor_arm",): "sudo /home/pi/.firewalla/config/post_main.sh 2>/dev/null",
    # FIX (c) raw readbacks for the poison gate. Unlike lease_observe (which strips
    # the IPv4 out), these RETURN THE RAW stdout so flip.evaluate_wan_poison can see
    # the /PREFIX and the route table — the /1-vs-/32 signal + a surviving 0.0.0.0/1
    # poison route the bare-IP read discards. Pure READS — never mutate the lease.
    ("wan_addr_cidr",): f'{_FW_WAN_DEV}; ip -4 -o addr show dev "$WAN" 2>/dev/null',
    ("wan_routes",): "ip -4 route show 2>/dev/null",
}

# The tags that READ + RETURN the FIRST IPv4 in stdout (lease_observe). The rest are
# fire-and-confirm mutations, EXCEPT the raw-readback tags below.
_FW_READBACK_TAGS = frozenset({("lease_observe",)})

# FIX (c): tags that READ + RETURN the RAW stdout verbatim (prefix + route table
# preserved). These must be checked BEFORE _FW_READBACK_TAGS so the IPv4-extracting
# parse never discards the /PREFIX + routes the poison gate needs.
_FW_RAW_READBACK_TAGS = frozenset({("wan_addr_cidr",), ("wan_routes",)})


def _fw_mutate_via_ssh(gateway: str, key: str, tag: tuple[str, ...]) -> str:
    """Fire one mutating single-NAT firerouter op over the fw key; fail-closed.

    Runs the tag's real remote command on the Firewalla and RAISES
    :class:`RuntimeError` on any failure (the subprocess could not spawn, timed
    out, or the remote command returned non-zero) — NEVER a silent "". A
    swallowed failure on the apply path would report a green cutover while the
    WAN was never switched and the rails' rollback never fired. For a read-back
    tag (``lease_observe``) returns the parsed downstream WAN IP (the first IPv4
    in stdout) so the flip can classify the lease; for the others returns "" on
    success (the orchestrator inspects only that they did not raise).
    """
    remote = _FW_MUTATING_REMOTE[tag]
    argv = _fw_ssh_argv(gateway, key, remote)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=30, check=False
        )
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        msg = f"Firewalla single-NAT op {tag[0]!r} failed: SSH transport error ({exc})"
        raise RuntimeError(msg) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        msg = f"Firewalla single-NAT op {tag[0]!r} failed (ssh exit {proc.returncode}): {detail}"
        raise RuntimeError(msg)
    if tag in _FW_RAW_READBACK_TAGS:
        # FIX (c): the poison gate needs the prefix + route table — return verbatim.
        return proc.stdout
    if tag in _FW_READBACK_TAGS:
        for line in proc.stdout.splitlines():
            s = line.strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", s):
                return s
        return ""  # readback op succeeded but the box reported no lease yet
    return ""


def make_real_runner(*, fw_gateway: str | None, fw_key: str | None) -> Runner:
    """A Runner that serves fw_wan_ip/fw_wan_mac from one cached Firewalla SSH
    probe, fires the four single-NAT mutating ops as real firerouter SSH commands,
    and delegates all remaining (host-side probe) tags to real_runner.

    Fail-closed on the apply path (the council BLOCK that earned this fix): a
    mutating single-NAT tag (``wan_dhcp`` / ``lease_observe`` / ``dhcp_release`` /
    ``armor_arm``) with no fw gateway+key RAISES, and any unknown/empty tag RAISES
    — never a silent "" that would report a green cutover the box never received.
    The read tags (``fw_wan_ip`` / ``fw_wan_mac``) and the host-side probe tags
    keep returning "" on absence (the read/classify path handles empty).
    """
    cache: dict[str, tuple[str, str]] = {}

    def runner(tag: tuple[str, ...]) -> str:
        if tag in (("fw_wan_ip",), ("fw_wan_mac",)):
            if "fw" not in cache:
                cache["fw"] = (
                    firewalla_wan_via_ssh(fw_gateway, fw_key) if fw_gateway and fw_key else ("", "")
                )
            ip, mac = cache["fw"]
            return ip if tag == ("fw_wan_ip",) else mac
        if tag in _FW_MUTATING_REMOTE:
            if not (fw_gateway and fw_key):
                msg = (
                    f"single-NAT op {tag[0]!r} needs a Firewalla gateway + SSH key to fire; "
                    "none resolved — refusing a silent no-op on the apply path"
                )
                raise RuntimeError(msg)
            return _fw_mutate_via_ssh(fw_gateway, fw_key, tag)
        # Host-side probe tags (traceroute/route/ifconfig/...) are real_runner's.
        # A tag real_runner ALSO does not know is an unknown apply-path tag — a
        # hard failure, never a silent "" (the council-blocked no-op).
        if tag not in _REAL_RUNNER_TAGS:
            msg = (
                f"unknown runner tag {tag!r} — refusing a silent no-op. "
                "Known tags: fw_wan_ip/fw_wan_mac, the single-NAT mutating ops "
                f"{sorted(t[0] for t in _FW_MUTATING_REMOTE)}, "
                f"and host probes {sorted(t[0] for t in _REAL_RUNNER_TAGS)}."
            )
            raise RuntimeError(msg)
        return real_runner(tag)

    return runner
