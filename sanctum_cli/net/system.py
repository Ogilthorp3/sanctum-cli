from __future__ import annotations

import re
import subprocess
import urllib.error
import urllib.request

# Tag -> argv for host-side probes that exist on any Mac. The Firewalla WAN
# tags ("fw_wan_ip", "fw_wan_mac") are NOT resolved here in v1 — they are a
# documented follow-up seam: a future caller will compose a runner that fetches
# them from the Firewalla bridge/SSH and falls back to this real_runner for the
# host probes. Until then, unknown tags return "" (callers handle that path).
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
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (subprocess.TimeoutExpired, OSError):
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
