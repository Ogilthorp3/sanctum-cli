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


def firewalla_wan_via_ssh(gateway: str, key: str, user: str = "pi") -> tuple[str, str]:
    """SSH to the Firewalla (key-only, read-only) and return (wan_ip, wan_mac).

    Returns ("", "") on any failure. Reads the WAN interface's MAC + IPv4.
    """
    remote = (
        "D=$(ip -o route get 1.1.1.1 2>/dev/null | grep -oE 'dev [a-z0-9.]+' | cut -d' ' -f2); "
        "cat /sys/class/net/$D/address 2>/dev/null; "
        "ip -o -4 addr show $D 2>/dev/null | awk '{print $4}' | cut -d/ -f1"
    )
    argv = [
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


_FW_PORT_MBPS = re.compile(r"^(\d+)")


def parse_fw_ports(blob: str) -> list[tuple[str, int]]:
    """Parse the Firewalla port probe into (portname, link-Mbps) rows.

    The remote emits one ``name<TAB>mbps`` row per WAN/LAN port, reading the
    link rate from ``/sys/class/net/<dev>/speed`` (or the leading integer of an
    ``ethtool`` speed field, e.g. ``2500Mb/s``). A row whose speed is missing,
    non-positive (the kernel reports ``-1`` for a link-down iface), or malformed
    is dropped — only known, up ports become hops. Order is preserved.
    """
    ports: list[tuple[str, int]] = []
    for line in blob.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name = parts[0].strip()
        m = _FW_PORT_MBPS.match(parts[1].strip())
        if not name or m is None:
            continue
        mbps = int(m.group(1))
        if mbps > 0:
            ports.append((name, mbps))
    return ports


def parse_wan_kind(text: str) -> str:
    """Classify the WAN uplink kind from a probe blob → ``pppoe``/``dhcp``/``static``/``""``.

    PPPoE is detected first (it is the load-bearing signal — it drives the
    CPU-bound advice band): a ``pppN`` interface in ``ip link show type ppp``
    output, or a bare ``pppoe`` token. Otherwise a bare ``dhcp`` / ``static``
    token classifies the addressing method. An empty, whitespace-only, or
    unrecognized blob returns ``""`` (undetermined — no band emitted).
    """
    low = text.lower()
    if "pppoe" in low or re.search(r"\bppp\d+\b", low):
        return "pppoe"
    for token in ("dhcp", "static"):
        if re.search(rf"\b{token}\b", low):
            return token
    return ""


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


def make_real_runner(*, fw_gateway: str | None, fw_key: str | None) -> Runner:
    """A Runner that serves fw_wan_ip/fw_wan_mac from one cached Firewalla SSH
    probe (only if a gateway + key are available) and delegates all other tags
    to real_runner."""
    cache: dict[str, tuple[str, str]] = {}

    def runner(tag: tuple[str, ...]) -> str:
        if tag in (("fw_wan_ip",), ("fw_wan_mac",)):
            if "fw" not in cache:
                cache["fw"] = (
                    firewalla_wan_via_ssh(fw_gateway, fw_key) if fw_gateway and fw_key else ("", "")
                )
            ip, mac = cache["fw"]
            return ip if tag == ("fw_wan_ip",) else mac
        return real_runner(tag)

    return runner
