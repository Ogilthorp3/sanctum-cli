"""Hive service principal (wave-1) — ownership checks for control-plane daemons.

The hub runs proxyd / force-flow / memory-vault as LaunchDaemons under a dedicated
local user ``sanctum`` so the control plane does not ride the operator's GUI
login session. Family Pass (CLI-only) installs never create this user; those
installs treat the probes as not-applicable.

Install (sudo, once per hub)::

    sanctum service-user install
    # → ~/.sanctum/scripts/service-user/install-on-new-hub.sh

Status / check (no sudo)::

    sanctum service-user status
    sanctum service-user check   # exit 1 on fail — self-test / CI
"""

from __future__ import annotations

import os
import pwd
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

WAVE1_LABELS: tuple[str, ...] = (
    "com.sanctum.proxyd",
    "com.sanctum.force-flow",
    "com.sanctum.memory-vault",
)

# Process argv patterns (egrep-style) for each wave-1 unit.
_PROCESS_PATTERNS: dict[str, str] = {
    "com.sanctum.proxyd": r"bin/proxyd$|/proxyd$",
    "com.sanctum.force-flow": r"force_flow\.py",
    "com.sanctum.memory-vault": r"sanctum-memory-vault",
}

INSTALL_SCRIPT = Path.home() / ".sanctum/scripts/service-user/install-on-new-hub.sh"
STATUS_SCRIPT = Path.home() / ".sanctum/scripts/service-user/status.sh"
DAEMON_DIR = Path("/Library/LaunchDaemons")
SERVICE_USERNAME = "sanctum"


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str = ""
    skip: bool = False


@dataclass
class Wave1Report:
    """Result of a wave-1 ownership / health check."""

    applicable: bool
    items: list[CheckItem] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        if not self.applicable:
            return True
        return all(i.ok or i.skip for i in self.items) and any(
            i.ok and not i.skip for i in self.items
        )

    @property
    def failed(self) -> list[CheckItem]:
        return [i for i in self.items if not i.ok and not i.skip]


def haus_tier_present() -> bool:
    """True when this Mac has haus-operator artifacts (not Family Pass only)."""
    markers = [
        Path.home() / ".sanctum/sanctum-proxy",
        Path.home() / ".sanctum/manifests",
        DAEMON_DIR / "com.sanctum.proxyd.plist",
    ]
    return any(p.exists() for p in markers)


def service_user_exists(name: str = SERVICE_USERNAME) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _plist_username(plist: Path) -> str | None:
    if not plist.is_file():
        return None
    try:
        # PlistBuddy is more reliable for Apple plists than ElementTree
        out = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c", "Print :UserName", str(plist)],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    # fallback: rough XML parse
    try:
        root = ET.parse(plist).getroot()
        # flat dict under <dict>
        children = list(root.find("dict") or [])
        for i, el in enumerate(children):
            if el.tag == "key" and (el.text or "") == "UserName":
                if i + 1 < len(children) and children[i + 1].tag == "string":
                    return children[i + 1].text
    except (ET.ParseError, OSError):
        return None
    return None


def _process_owner(pattern: str) -> str | None:
    try:
        out = subprocess.run(
            ["ps", "-Ao", "user,args"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    import re

    rx = re.compile(pattern)
    for line in out.stdout.splitlines():
        if "egrep" in line or "grep" in line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        user, args = parts[0], parts[1]
        if rx.search(args):
            return user
    return None


def _http_status(url: str, timeout: float = 3.0, tls: bool = False) -> int:
    import ssl
    import urllib.error
    import urllib.request

    ctx = None
    if tls:
        ctx = ssl._create_unverified_context()  # noqa: S323 — loopback health only
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


def check_wave1(*, require_user: bool = True) -> Wave1Report:
    """Check wave-1 ownership and health.

    On CLI-only installs (no haus markers), returns ``applicable=False``.
    """
    if not haus_tier_present():
        return Wave1Report(
            applicable=False,
            reason="CLI-only install — service principal not expected",
        )

    items: list[CheckItem] = []

    if require_user:
        if service_user_exists():
            items.append(CheckItem("sanctum user exists", True))
        else:
            items.append(
                CheckItem(
                    "sanctum user exists",
                    False,
                    "missing — run: sanctum service-user install",
                )
            )

    for label in WAVE1_LABELS:
        plist = DAEMON_DIR / f"{label}.plist"
        if not plist.is_file():
            items.append(CheckItem(f"plist {label}", False, "not installed in /Library/LaunchDaemons"))
            continue
        items.append(CheckItem(f"plist {label}", True, str(plist)))
        uname = _plist_username(plist)
        if uname == SERVICE_USERNAME:
            items.append(CheckItem(f"UserName={SERVICE_USERNAME}: {label}", True))
        else:
            items.append(
                CheckItem(
                    f"UserName={SERVICE_USERNAME}: {label}",
                    False,
                    f"got {uname!r}",
                )
            )

    for label, pat in _PROCESS_PATTERNS.items():
        owner = _process_owner(pat)
        short = label.removeprefix("com.sanctum.")
        if owner is None:
            items.append(CheckItem(f"process {short}", False, "not running"))
        elif owner == SERVICE_USERNAME:
            items.append(CheckItem(f"process {short} owner", True, owner))
        else:
            items.append(
                CheckItem(
                    f"process {short} owner",
                    False,
                    f"got {owner!r}, expected {SERVICE_USERNAME}",
                )
            )

    # Health endpoints (any non-zero HTTP for TLS proxyd; 200 for force-flow)
    ff = _http_status("http://127.0.0.1:4077/health")
    if ff == 200:
        items.append(CheckItem("force-flow /health", True, "HTTP 200"))
    else:
        items.append(CheckItem("force-flow /health", False, f"HTTP {ff}"))

    # proxyd — any response means alive
    px = _http_status("https://127.0.0.1:4040/v1/models", tls=True)
    if px != 0:
        items.append(CheckItem("proxyd TLS", True, f"HTTP {px}"))
    else:
        items.append(CheckItem("proxyd TLS", False, "no response"))

    vault = _http_status("http://127.0.0.1:42069/")
    if vault != 0:
        items.append(CheckItem("memory-vault :42069", True, f"HTTP {vault}"))
    else:
        # vault may use different port on some hubs — skip hard fail if process ok
        items.append(
            CheckItem(
                "memory-vault :42069",
                False,
                "no response (instance.yaml port may differ)",
            )
        )

    return Wave1Report(applicable=True, items=items)


def install_script_path() -> Path:
    return INSTALL_SCRIPT


def run_install(*, dry_run: bool = False) -> int:
    """Run the hub install script (requires admin via sudo).

    Returns the subprocess exit code. Raises FileNotFoundError if the
    sanctum-config scripts are not present on this machine.
    """
    script = install_script_path()
    if not script.is_file():
        raise FileNotFoundError(
            f"missing {script} — clone/sync sanctum-config so "
            "~/.sanctum/scripts/service-user/ exists"
        )
    if dry_run:
        return 0
    # Preserve interactive TTY so sudo can prompt.
    cmd = ["sudo", "/bin/bash", str(script)]
    return subprocess.call(cmd)  # noqa: S603
