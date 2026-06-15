from __future__ import annotations

import re
import subprocess
import urllib.error
import urllib.request
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
}


def real_runner(tag: tuple[str, ...]) -> str:
    if tag == ("public_ip",):
        return _http_text("https://api.ipify.org")
    argv = _COMMANDS.get(tag)
    if argv is None:
        return ""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=20, check=False)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return ""
    return proc.stdout


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
        "ssh", "-i", key,
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        f"{user}@{gateway}", remote,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=12, check=False)
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


def make_real_runner(*, fw_gateway: str | None, fw_key: str | None) -> Runner:
    """A Runner that serves fw_wan_ip/fw_wan_mac from one cached Firewalla SSH
    probe (only if a gateway + key are available) and delegates all other tags
    to real_runner."""
    cache: dict[str, tuple[str, str]] = {}

    def runner(tag: tuple[str, ...]) -> str:
        if tag in (("fw_wan_ip",), ("fw_wan_mac",)):
            if "fw" not in cache:
                cache["fw"] = (
                    firewalla_wan_via_ssh(fw_gateway, fw_key)
                    if fw_gateway and fw_key
                    else ("", "")
                )
            ip, mac = cache["fw"]
            return ip if tag == ("fw_wan_ip",) else mac
        return real_runner(tag)

    return runner
