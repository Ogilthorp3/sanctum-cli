"""``sanctum node`` — productized satellite onboarding (new Mac in the closet).

The haus grows one Mac at a time: a new machine lands in a closet, the operator
sits at the console laptop, and three commands take it from "sealed box on the
shelf" to "adopted satellite the console can drive over SSH":

* ``scan``             — read-only discovery: which Macs are visible (tailnet +
  LAN), which already answer SSH ("adoptable now") and which still need a human
  at their keyboard ("needs bootstrap").
* ``bootstrap-script`` — generate the ONE script to run at the new Mac's own
  keyboard. It turns on Remote Login, authorizes the console's automation key,
  and hardens the machine for headless duty. The script content is the
  field-validated procedure from the chalet M1 Mini bring-up — its semantics
  are the contract, do not "improve" them casually.
* ``adopt``            — the console side of the hand-off: poll SSH until the
  bootstrap lands, then drive the honest-verify scorecard (L1 reach → L6
  register) and record the node under ``nodes.<name>`` in instance.yaml. Every
  ✓ comes from a real probe over the wire — never from "we asked for it".

Every subprocess / socket touchpoint is a module-level seam
(:func:`_run_local`, :func:`_probe_tcp`, :func:`_run_ssh`,
:func:`_generate_automation_key`) so the whole surface is unit-testable with
zero live network, zero keygen, and zero SSH.
"""

from __future__ import annotations

import concurrent.futures
import enum
import getpass
import hashlib
import ipaddress
import re
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from sanctum_cli import config
from sanctum_cli.errors import LocalError, NetworkError, SanctumError

if TYPE_CHECKING:
    from collections.abc import Callable

console = Console()
err_console = Console(stderr=True)

node_app = typer.Typer(help="Satellite-Mac onboarding — scan, bootstrap hand-off, adopt.")


def _report(exc: SanctumError) -> None:
    """Pretty-print a SanctumError to stderr with its optional fix suggestion.

    Mirrors ``net._report`` so the node sub-app reports failures the same way the
    rest of the CLI does (it cannot import from ``cli`` without a cycle).
    """
    err_console.print(f"[bold red]error:[/] {exc.message}")
    if exc.fix:
        err_console.print(f"[dim]fix:[/] {exc.fix}")


# ─── impure seams (tests monkeypatch these; nothing else shells out) ──

#: Where the Tailscale CLI lives when the app is installed but not on PATH.
_TAILSCALE_APP_BIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

#: Socket-connect timeout for a tailnet peer probe (spec: 2s).
_PROBE_TIMEOUT_S = 2.0
#: Socket-connect timeout for a LAN host probe (LAN answers fast or not at all).
_LAN_PROBE_TIMEOUT_S = 1.5
#: Hard wall-clock cap on the whole parallel probe pass (spec: ~10s).
_SCAN_BUDGET_S = 10.0

_SSH_PORT = 22
_SCREEN_SHARING_PORT = 5900

#: Seconds between SSH-reachability polls while ``adopt --wait`` is counting down.
_ADOPT_POLL_INTERVAL_S = 3.0
#: Per-command timeout for the adopt probes (each is one short remote command).
_SSH_COMMAND_TIMEOUT_S = 20


def _run_local(argv: list[str], *, timeout: int = 15) -> tuple[int, str]:
    """Run a local command; return ``(returncode, stdout)``. Never raises.

    The impure boundary for every local shell-out in this module (``tailscale
    status``, ``arp -an``, ``ssh-keygen``). Any OS/subprocess failure reads as
    ``(1, "")`` so callers degrade (skip a source, raise a typed error) instead
    of crashing. Tests monkeypatch THIS function to script outputs.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return (1, "")
    return (proc.returncode, proc.stdout)


def _probe_tcp(host: str, port: int, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """True iff a TCP connect to ``host:port`` succeeds within ``timeout``.

    Read-only reachability probe (connect + close, no bytes sent). A module-level
    seam so scan/adopt tests script reachability without touching a socket.
    """
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _run_ssh(host: str, user: str, command: str, *, timeout: int = _SSH_COMMAND_TIMEOUT_S) -> tuple[int, str]:
    """Run one command on the satellite over SSH; return ``(returncode, stdout)``.

    Hardened transport mirroring ``net._firewalla_guardian_epoch``: BatchMode (a
    password prompt must fail, never hang), publickey-only, accept-new host keys,
    bounded connect — plus ``IdentitiesOnly`` pinned to the sanctum automation
    key so an agent full of other identities can't shadow the probe. Never
    raises; any transport failure reads as ``(255, "")`` (the ssh convention).
    """
    argv = [
        "ssh",
        "-i",
        str(_automation_key_path()),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=5",
        f"{user}@{host}",
        command,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return (255, "")
    return (proc.returncode, proc.stdout)


# ─── automation key (the console's identity on every satellite) ───────


def _automation_key_path() -> Path:
    """The console's automation private key (its ``.pub`` sibling gets embedded).

    A function (not a constant) so tests point the whole module at a tmp dir
    with one monkeypatch.
    """
    return Path.home() / ".ssh" / "sanctum_automation"


def _generate_automation_key(priv: Path) -> None:
    """Generate the ed25519 automation pair at ``priv`` / ``priv.pub`` — a seam.

    First-run only (``bootstrap-script`` calls it when no ``.pub`` exists). The
    comment names the console + date so a satellite's authorized_keys reads as an
    audit trail. Raises ``LocalError`` when ssh-keygen fails — a bootstrap script
    with no key to embed is useless, so this is not best-effort.
    """
    host = (socket.gethostname() or "console").split(".")[0]
    comment = f"sanctum-automation-{host}-{date.today().isoformat()}"
    priv.parent.mkdir(parents=True, exist_ok=True)
    rc, _out = _run_local(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(priv), "-C", comment]
    )
    if rc != 0:
        msg = f"ssh-keygen failed generating the automation key at {priv}"
        raise LocalError(msg, fix="check ~/.ssh exists and is writable, then re-run")


def _ensure_automation_pubkey() -> str:
    """Return the automation public-key line, generating the pair on first run.

    The one place the key pair is born: absent ``.pub`` → generate (via the
    :func:`_generate_automation_key` seam), then read. An unreadable/empty
    ``.pub`` after that is a real local failure and raises ``LocalError``.
    """
    pub = _automation_key_path().with_suffix(".pub")
    if not pub.exists():
        _generate_automation_key(_automation_key_path())
    try:
        text = pub.read_text(encoding="utf-8").strip()
    except OSError as exc:
        msg = f"cannot read the automation public key at {pub}"
        raise LocalError(msg, fix="regenerate it: delete the pair and re-run") from exc
    if not text:
        msg = f"automation public key at {pub} is empty"
        raise LocalError(msg, fix="regenerate it: delete the pair and re-run")
    return text


# ─── scan (read-only discovery) ───────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """One discovered machine: where we saw it and what its ports said."""

    name: str
    host: str
    source: str  # "tailnet" | "LAN"
    online: bool
    ssh_open: bool | None  # None = not probed (offline peer)
    screen_open: bool | None  # None = not probed (tailnet rows probe SSH only)


def _tailscale_bin() -> str | None:
    """Resolve the Tailscale CLI: PATH first, then the app-bundle binary.

    None when neither exists — scan then skips the tailnet source with a note
    (no Tailscale is a normal haus state, not an error).
    """
    import shutil

    found = shutil.which("tailscale")
    if found:
        return found
    if Path(_TAILSCALE_APP_BIN).exists():
        return _TAILSCALE_APP_BIN
    return None


def parse_tailscale_status(text: str) -> list[Candidate]:
    """Parse ``tailscale status`` plain output into macOS peer candidates.

    Each peer line is ``<ip> <hostname> <owner> <os> <status…>``; comment lines
    (``#``, the health-check footer) and non-macOS peers are dropped — this
    command adopts Macs, not phones. A peer whose status column starts with
    ``offline`` is kept but marked offline so the table can say so honestly
    instead of calling an unreachable box "needs bootstrap". Ports are not
    probed here — the caller owns the (parallel, bounded) probe pass.
    """
    peers: list[Candidate] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        ip, hostname, _owner, os_name = parts[0], parts[1], parts[2], parts[3]
        status = parts[4] if len(parts) > 4 else ""
        if os_name != "macOS":
            continue
        peers.append(
            Candidate(
                name=hostname,
                host=ip,
                source="tailnet",
                online=not status.startswith("offline"),
                ssh_open=None,
                screen_open=None,
            )
        )
    return peers


_ARP_LINE_RE = re.compile(r"\((\d{1,3}(?:\.\d{1,3}){3})\) at ([0-9a-fA-F:]+|\(incomplete\))")


def parse_arp_hosts(text: str) -> list[str]:
    """Extract probeworthy IPv4 addresses from ``arp -an`` output.

    Keeps only entries with a resolved MAC (an ``(incomplete)`` row is a ghost)
    and drops non-host noise: multicast, loopback, link-local, and the ``.255``
    broadcast convention. Order-preserving, deduped.
    """
    hosts: list[str] = []
    for m in _ARP_LINE_RE.finditer(text):
        ip_s, mac = m.group(1), m.group(2)
        if mac == "(incomplete)":
            continue
        try:
            addr = ipaddress.ip_address(ip_s)
        except ValueError:
            continue
        if addr.is_multicast or addr.is_loopback or addr.is_link_local:
            continue
        if ip_s.endswith(".255"):  # subnet unknown; the convention is close enough
            continue
        if ip_s not in hosts:
            hosts.append(ip_s)
    return hosts


def _probe_ports(
    targets: list[tuple[str, int]], *, timeout: float, budget: float
) -> dict[tuple[str, int], bool]:
    """Probe every (host, port) in parallel under a hard wall-clock budget.

    Unfinished probes when the budget expires read as closed (fail-closed: scan
    may under-report an adoptable Mac, never hang). The pool is released without
    waiting — each straggler thread self-bounds on its socket timeout.
    """
    results: dict[tuple[str, int], bool] = dict.fromkeys(targets, False)
    if not targets:
        return results
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(targets)))
    try:
        futures = {pool.submit(_probe_tcp, h, p, timeout): (h, p) for h, p in targets}
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=budget):
                results[futures[fut]] = bool(fut.result())
        except TimeoutError:
            pass  # budget spent — the rest stay False
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _verdict(c: Candidate) -> str:
    """One-word adoption verdict for the scan table."""
    if not c.online:
        return "offline"
    if c.ssh_open:
        return "adoptable now"
    return "needs bootstrap"


def _render_scan(candidates: list[Candidate]) -> None:
    """Render discovery as one table: who's there, what answers, what's next."""

    def yn(v: bool | None) -> str:
        if v is None:
            return "[dim]-[/]"
        return "[green]open[/]" if v else "[dim]closed[/]"

    table = Table(title="Candidate satellite Macs", title_justify="left")
    table.add_column("name / IP", overflow="fold")
    table.add_column("source", no_wrap=True)
    table.add_column("ssh", no_wrap=True)
    table.add_column("screen-sharing", no_wrap=True)
    table.add_column("verdict", no_wrap=True)
    for c in candidates:
        label = f"{escape(c.name)}  [dim]{escape(c.host)}[/]" if c.name != c.host else escape(c.host)
        verdict = _verdict(c)
        style = {"adoptable now": "green", "needs bootstrap": "yellow", "offline": "dim"}[verdict]
        table.add_row(label, c.source, yn(c.ssh_open), yn(c.screen_open), f"[{style}]{verdict}[/]")
    console.print(table)
    console.print(
        "[dim]adoptable now → `sanctum node adopt <name-or-ip>` · "
        "needs bootstrap → `sanctum node bootstrap-script` and run it at that Mac's keyboard[/]"
    )


@node_app.command("scan", help="Discover candidate satellite Macs (read-only, makes no changes).")
def node_scan() -> None:
    """Read-only discovery across both visibility planes.

    Tailnet: ``tailscale status`` (PATH, then the app-bundle binary) filtered to
    macOS peers; each *online* peer gets an SSH :22 probe. LAN: a bounded ARP
    read (``arp -an`` — mDNS via ``dns-sd`` is too flaky to script) with :22 and
    :5900 probed on every live host. All probes run in parallel under the
    ~10s hard cap; nothing here mutates anything anywhere.
    """
    candidates: list[Candidate] = []

    # Tailnet peers (skipped with a note when no Tailscale is installed).
    ts = _tailscale_bin()
    if ts is None:
        console.print("[dim]tailnet: no tailscale binary found — skipping[/]")
        peers: list[Candidate] = []
    else:
        rc, out = _run_local([ts, "status", "--self=false"])
        peers = parse_tailscale_status(out) if rc == 0 else []
        if rc != 0:
            console.print("[dim]tailnet: `tailscale status` failed — skipping[/]")

    # LAN hosts from the ARP cache (only machines that recently talked to us).
    _rc_arp, arp_out = _run_local(["arp", "-an"])
    lan_hosts = parse_arp_hosts(arp_out)

    # One parallel probe pass for everything, under the hard budget. Tailnet rows
    # probe SSH only (the adoption question); LAN rows probe :22 and :5900.
    targets: list[tuple[str, int]] = [
        (p.host, _SSH_PORT) for p in peers if p.online
    ] + [(h, port) for h in lan_hosts for port in (_SSH_PORT, _SCREEN_SHARING_PORT)]
    opened = _probe_ports(
        targets,
        timeout=_LAN_PROBE_TIMEOUT_S if not peers else _PROBE_TIMEOUT_S,
        budget=_SCAN_BUDGET_S,
    )

    for p in peers:
        ssh = opened.get((p.host, _SSH_PORT)) if p.online else None
        candidates.append(
            Candidate(
                name=p.name,
                host=p.host,
                source="tailnet",
                online=p.online,
                ssh_open=ssh,
                screen_open=None,
            )
        )
    for h in lan_hosts:
        candidates.append(
            Candidate(
                name=h,
                host=h,
                source="LAN",
                online=True,
                ssh_open=opened.get((h, _SSH_PORT), False),
                screen_open=opened.get((h, _SCREEN_SHARING_PORT), False),
            )
        )

    if not candidates:
        console.print("nothing discovered — no tailnet peers, and the ARP cache is empty.")
        return
    _render_scan(candidates)


# ─── bootstrap-script (the one run at the new Mac's keyboard) ─────────

# The script below is the FIELD-VALIDATED procedure (chalet M1 Mini bring-up):
#   a. Remote Login on            — the console's way in
#   b. authorized_keys append     — idempotent (grep -qF guard), 700/600 perms
#   c. headless reliability       — restart-on-power-failure, never sleep,
#                                   pmset sleep/disksleep/powernap 0 + womp 1
#   d. passwordless sudo          — ONLY with --sudo-nopasswd; sudoers.d
#                                   drop-in, 0440, visudo-validated with a
#                                   remove-on-invalid guard (never brick sudo)
# Keep the semantics; the wrapper text may evolve, the commands must not.
# bash-3.2-safe on purpose: /bin/bash on macOS is 3.2 and always present.

_SUDO_NOPASSWD_STEP = """\
echo "[4/4] passwordless sudo for automation (--sudo-nopasswd)"
printf '%s ALL=(ALL) NOPASSWD: ALL\\n' "$(whoami)" | sudo tee /etc/sudoers.d/sanctum-automation >/dev/null
sudo chmod 0440 /etc/sudoers.d/sanctum-automation
if ! sudo visudo -c -f /etc/sudoers.d/sanctum-automation >/dev/null; then
  sudo rm -f /etc/sudoers.d/sanctum-automation
  echo "sudoers entry failed validation - removed, sudo unchanged" >&2
  exit 1
fi
"""

_SUDO_SKIPPED_STEP = """\
echo "[4/4] passwordless sudo: skipped (re-generate with --sudo-nopasswd to enable)"
"""


def render_bootstrap_script(pubkey: str, *, sudo_nopasswd: bool) -> str:
    """Render the one-run bootstrap script with ``pubkey`` embedded.

    Pure (string in → string out) so tests pin the exact field-validated
    semantics without touching a key pair or a filesystem. The pubkey line is
    embedded single-quoted; ssh public keys are base64 + a plain comment, so a
    quote in one would mean a corrupted key — refuse rather than mis-embed.
    """
    if "'" in pubkey:
        msg = "automation public key contains a quote — refusing to embed it"
        raise LocalError(msg, fix="regenerate the key: delete the pair and re-run")
    sudo_step = _SUDO_NOPASSWD_STEP if sudo_nopasswd else _SUDO_SKIPPED_STEP
    return f"""\
#!/bin/bash
# sanctum node bootstrap — run ONCE in Terminal at the new Mac's own keyboard.
# Generated by `sanctum node bootstrap-script`. Idempotent: safe to re-run.
# You will be asked for this Mac's admin password (sudo).
set -euo pipefail

PUBKEY='{pubkey}'

echo "[1/4] Remote Login (SSH) on"
sudo systemsetup -setremotelogin on

echo "[2/4] authorize the console's sanctum automation key"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
grep -qF "$PUBKEY" "$HOME/.ssh/authorized_keys" || printf '%s\\n' "$PUBKEY" >> "$HOME/.ssh/authorized_keys"

echo "[3/4] headless reliability (restart on power failure, never sleep, wake-on-LAN)"
sudo systemsetup -setrestartpowerfailure on
sudo systemsetup -setcomputersleep Never
sudo pmset -a sleep 0 disksleep 0 powernap 0 womp 1

{sudo_step}
echo ""
echo "bootstrap complete. This Mac's IPv4 addresses:"
ipconfig getifaddr en0 2>/dev/null || true
ipconfig getifaddr en1 2>/dev/null || true
echo "Back at the console, finish with:  sanctum node adopt <ip-above>"
"""


# ─── pkg installer (the mom-friendly, double-clickable hand-off) ──────
#
# `--format pkg` (the default) is the Apple-like hand-off: a double-clickable
# macOS installer whose postinstall runs the SAME field-validated bootstrap as
# root via the native Installer — no Terminal, no typed sudo. The curl/USB script
# above is kept as the `--format script` developer fallback. Every shell-out
# (pkgbuild, productsign, security, open) is a module-level seam so the whole
# surface is unit-tested with fakes and no real pkg is built.

#: pkgbuild package identifier for the node-setup installer.
_PKG_IDENTIFIER = "haus.sanctum.node-setup"


@dataclass(frozen=True)
class PkgResult:
    """Outcome of building the installer: where it landed and whether it's signed."""

    path: Path
    signed: bool
    identity: str | None


def pairing_code(pubkey: str) -> str:
    """A stable 6-digit pairing code derived from the automation key fingerprint.

    Deterministic (same key → same code, so the console can print it and a
    follow-on confirm UI can check it) and key-bound (different keys → different
    codes with overwhelming probability). Derived from the key's base64 blob — the
    same field :func:`_probe_key_hygiene` matches on — so it tracks the key
    material, not the comment/whitespace. Pure: no I/O.
    """
    parts = pubkey.split()
    blob = parts[1] if len(parts) >= 2 else pubkey.strip()
    digest = hashlib.sha256(blob.encode("utf-8")).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


_PKG_POSTINSTALL_SUDO = """\
echo "[4/4] passwordless sudo for automation (--sudo-nopasswd)"
printf '%s ALL=(ALL) NOPASSWD: ALL\\n' "$CU" > /etc/sudoers.d/sanctum-automation
chmod 0440 /etc/sudoers.d/sanctum-automation
if ! visudo -c -f /etc/sudoers.d/sanctum-automation >/dev/null 2>&1; then
  rm -f /etc/sudoers.d/sanctum-automation
  echo "sudoers entry failed validation - removed, sudo unchanged" >&2
fi
"""

_PKG_POSTINSTALL_NOSUDO = """\
echo "[4/4] passwordless sudo: skipped (rebuild with --sudo-nopasswd to enable)"
"""


def render_postinstall(pubkey: str, *, sudo_nopasswd: bool, code: str) -> str:
    """Render the ``.pkg`` ``postinstall`` script — runs as ROOT via the Installer.

    The double-click path: no Terminal, no typed sudo. Same field-validated steps
    as :func:`render_bootstrap_script`, adapted for a root postinstall — it resolves
    the logged-in **console user** (``stat -f%Su /dev/console`` with a root
    fallback) and that user's home (``dscl``), so the key lands in the human's
    ``~/.ssh`` (``chown``'d to them), never in root's home. ``code`` is stamped in a
    header marker so the artifact is tied to THIS console's key (the console prints
    the same pairing code). Idempotent, bash-3.2-safe, embeds no IP.
    """
    if "'" in pubkey:
        msg = "automation public key contains a quote — refusing to embed it"
        raise LocalError(msg, fix="regenerate the key: delete the pair and re-run")
    sudo_step = _PKG_POSTINSTALL_SUDO if sudo_nopasswd else _PKG_POSTINSTALL_NOSUDO
    return f"""\
#!/bin/bash
# sanctum-pairing: {code}
# sanctum node setup — pkg postinstall, runs as ROOT via the macOS Installer.
# Generated by `sanctum node bootstrap-script --format pkg`. No Terminal needed.
set -uo pipefail

# Resolve the logged-in console user — the key must NOT land in root's home.
CU=$(stat -f%Su /dev/console); [ -z "$CU" -o "$CU" = root ] && CU=$(ls -l /dev/console | awk '{{print $3}}')
HD=$(dscl . -read /Users/"$CU" NFSHomeDirectory 2>/dev/null | awk '{{print $2}}')
KEY='{pubkey}'

echo "[1/4] Remote Login (SSH) on"
systemsetup -setremotelogin on 2>/dev/null || true

echo "[2/4] authorize the console's sanctum automation key (user $CU)"
sudo -u "$CU" mkdir -p "$HD/.ssh"
chmod 700 "$HD/.ssh"
grep -qF "$KEY" "$HD/.ssh/authorized_keys" 2>/dev/null || echo "$KEY" >> "$HD/.ssh/authorized_keys"
chown "$CU" "$HD/.ssh/authorized_keys"
chmod 600 "$HD/.ssh/authorized_keys"

echo "[3/4] headless reliability (restart on power failure, never sleep, wake-on-LAN)"
systemsetup -setrestartpowerfailure on 2>/dev/null || true
systemsetup -setcomputersleep Never 2>/dev/null || true
pmset -a sleep 0 disksleep 0 powernap 0 womp 1 2>/dev/null || true

{sudo_step}exit 0
"""


def _pkg_version() -> str:
    """Installer version string — date-based (monotonic enough for ``pkgutil``)."""
    return date.today().strftime("%Y.%m.%d")


def _pkgbuild_argv(*, scripts_dir: Path, out_pkg: Path, version: str) -> list[str]:
    """The exact ``pkgbuild`` invocation (pure — argv only, nothing runs)."""
    return [
        "pkgbuild",
        "--nopayload",
        "--scripts",
        str(scripts_dir),
        "--identifier",
        _PKG_IDENTIFIER,
        "--version",
        version,
        str(out_pkg),
    ]


def _productsign_argv(*, identity: str, unsigned: Path, signed: Path) -> list[str]:
    """The exact ``productsign`` invocation (pure — argv only)."""
    return ["productsign", "--sign", identity, str(unsigned), str(signed)]


def _run_pkg_tool(argv: list[str]) -> tuple[int, str]:
    """Default pkg-tool seam: run pkgbuild/productsign; return (rc, output). Never raises."""
    return _run_local(argv, timeout=120)


def _find_installer_identity() -> str | None:
    """Return a 'Developer ID Installer' identity name if the keychain has one, else None.

    Reads ``security find-identity -v`` and returns the first Developer ID
    Installer common-name; None means the pkg ships unsigned (the command prints
    the sign+notarize note). A seam so tests drive both branches without a keychain.
    """
    rc, out = _run_local(["security", "find-identity", "-v"])
    if rc != 0:
        return None
    m = re.search(r'"(Developer ID Installer:[^"]+)"', out)
    return m.group(1) if m else None


def _stage_postinstall(postinstall: str) -> Path:
    """Write ``postinstall`` (0755) into a fresh temp scripts dir; return that dir.

    pkgbuild's ``--scripts`` wants a directory whose ``postinstall`` runs after
    install. A seam so tests assert the pkgbuild argv without writing a real script.
    """
    scripts_dir = Path(tempfile.mkdtemp(prefix="sanctum-node-pkg-"))
    script = scripts_dir / "postinstall"
    script.write_text(postinstall, encoding="utf-8")
    script.chmod(0o755)
    return scripts_dir


def _reveal_in_finder(path: Path) -> None:
    """Reveal ``path`` in Finder (``open -R``) — best-effort, never raises."""
    _run_local(["open", "-R", str(path)])


def build_pkg(
    postinstall: str,
    out_pkg: Path,
    *,
    version: str,
    pkg_builder: Callable[[list[str]], tuple[int, str]] | None = None,
    find_identity: Callable[[], str | None] | None = None,
    stage_scripts: Callable[[str], Path] | None = None,
) -> PkgResult:
    """Build (and, if possible, sign) the double-clickable node-setup ``.pkg``.

    Stages the postinstall, runs ``pkgbuild --nopayload`` via the injectable
    ``pkg_builder`` seam, then — only when ``find_identity`` reports a Developer ID
    Installer — ``productsign``s it (same seam). Returns where the deliverable
    landed and whether it was signed. The seams default to the module-level real
    implementations, resolved at call time, so a command test can monkeypatch
    ``_run_pkg_tool`` / ``_find_installer_identity`` / ``_stage_postinstall`` and a
    unit test can pass fakes directly — either way no real package is built.
    """
    run_tool = pkg_builder or _run_pkg_tool
    identify = find_identity or _find_installer_identity
    stage = stage_scripts or _stage_postinstall

    scripts_dir = stage(postinstall)
    identity = identify()
    if identity:
        unsigned = out_pkg.with_suffix(".unsigned.pkg")
        rc, out = run_tool(_pkgbuild_argv(scripts_dir=scripts_dir, out_pkg=unsigned, version=version))
        if rc != 0:
            raise LocalError(f"pkgbuild failed (rc {rc})", fix=f"run pkgbuild by hand — {out[:200]}")
        src, sout = run_tool(_productsign_argv(identity=identity, unsigned=unsigned, signed=out_pkg))
        if src != 0:
            raise LocalError(f"productsign failed (rc {src})", fix=f"check the signing identity — {sout[:200]}")
        return PkgResult(path=out_pkg, signed=True, identity=identity)
    rc, out = run_tool(_pkgbuild_argv(scripts_dir=scripts_dir, out_pkg=out_pkg, version=version))
    if rc != 0:
        raise LocalError(f"pkgbuild failed (rc {rc})", fix=f"run pkgbuild by hand — {out[:200]}")
    return PkgResult(path=out_pkg, signed=False, identity=None)


class _HandoffFormat(enum.StrEnum):
    """Hand-off artifact format: the mom-friendly pkg (default) or the dev script."""

    PKG = "pkg"
    SCRIPT = "script"


def _default_pkg_path() -> Path:
    """Default installer location — the console operator's Desktop (mom-friendly)."""
    desktop = Path.home() / "Desktop"
    base = desktop if desktop.is_dir() else Path.cwd()
    return base / "Sanctum-Node-Setup.pkg"


def _emit_script(pubkey: str, *, sudo_nopasswd: bool, out: Path | None, code: str) -> None:
    """The developer fallback: emit the curl/USB bootstrap script (stdout or ``--out``)."""
    script = render_bootstrap_script(pubkey, sudo_nopasswd=sudo_nopasswd)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(script, encoding="utf-8")
        out.chmod(0o755)
        console.print(f"[green]✓[/] wrote {escape(str(out))} [dim](0755)[/]")
        console.print(
            f"  [dim]pairing code on this Mac: [bold]{code}[/]  ·  developer fallback "
            "(prefer `--format pkg` for a double-click install).[/]"
        )
        console.print(
            "  [dim]get it to the new Mac (AirDrop / USB), run it in Terminal, "
            "then `sanctum node adopt <its-ip>` from here.[/]"
        )
        return
    # stdout carries ONLY the script so `... > bootstrap.sh` stays byte-clean.
    print(script, end="")
    err_console.print(
        f"[dim]pairing code on this Mac: {code}  ·  developer fallback "
        "(prefer `sanctum node bootstrap-script --format pkg`).[/]"
    )
    err_console.print(
        "[dim]save this, get it to the new Mac (AirDrop / USB), run it in Terminal, "
        "then `sanctum node adopt <its-ip>` from here.[/]"
    )


def _emit_pkg(pubkey: str, *, sudo_nopasswd: bool, out: Path | None, code: str) -> None:
    """Build the double-click installer and print mom-friendly delivery guidance."""
    postinstall = render_postinstall(pubkey, sudo_nopasswd=sudo_nopasswd, code=code)
    out_pkg = out if out is not None else _default_pkg_path()
    out_pkg.parent.mkdir(parents=True, exist_ok=True)
    result = build_pkg(postinstall, out_pkg, version=_pkg_version())
    console.print(f"[green]✓[/] built {escape(str(result.path))}")
    console.print(
        f"  [bold]Pairing code on this Mac: {code}[/]  "
        "[dim](confirm it matches on the new Mac when the setup app asks)[/]"
    )
    if result.signed:
        console.print(
            f"  [green]signed[/] with {escape(result.identity or '')} — installs with no Gatekeeper warning."
        )
    else:
        console.print(
            "  [yellow]unsigned[/] — the shipped product must be signed with a Developer ID "
            "Installer cert + notarized."
        )
        console.print("  [dim]to install this unsigned build now: right-click the .pkg → Open (once).[/]")
    console.print("  [bold]Deliver it:[/] AirDrop the .pkg to the new Mac, then double-click it there.")
    _reveal_in_finder(result.path)
    console.print(
        "  [dim]revealed in Finder · headless / no-Finder? use the dev fallback `--format script`.[/]"
    )
    console.print("  [dim]then, back here: `sanctum node adopt <its-ip>`.[/]")


@node_app.command(
    "bootstrap-script",
    help="Generate the new Mac's hand-off: a double-click .pkg (default) or a dev script.",
)
def node_bootstrap_script(
    fmt: Annotated[
        _HandoffFormat,
        typer.Option(
            "--format",
            help="pkg = double-click installer (mom-friendly, default); "
            "script = developer curl/USB fallback.",
        ),
    ] = _HandoffFormat.PKG,
    sudo_nopasswd: Annotated[
        bool,
        typer.Option(
            "--sudo-nopasswd",
            help="Also grant the satellite user passwordless sudo (sudoers.d, visudo-validated).",
        ),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Write here instead of the default (pkg → ~/Desktop/Sanctum-Node-Setup.pkg; "
            "script → stdout).",
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Generate the new Mac's hand-off, minting the automation key pair on first run.

    ``--format pkg`` (default) builds a signed-if-possible, double-clickable macOS
    installer whose ``postinstall`` runs the bootstrap as root via the native
    Installer — no Terminal, no typed sudo. ``--format script`` emits the developer
    curl/USB fallback (stdout, or ``--out`` for a 0755 file). Either way the console
    prints the 6-digit **pairing code** for this Mac's automation key. The embedded
    public key is ``~/.ssh/sanctum_automation.pub``, generated via ssh-keygen if this
    console has never bootstrapped a satellite before.
    """
    try:
        pubkey = _ensure_automation_pubkey()
        code = pairing_code(pubkey)
        if fmt is _HandoffFormat.SCRIPT:
            _emit_script(pubkey, sudo_nopasswd=sudo_nopasswd, out=out, code=code)
        else:
            _emit_pkg(pubkey, sudo_nopasswd=sudo_nopasswd, out=out, code=code)
    except SanctumError as exc:
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code)) from exc


# ─── adopt (poll SSH → L1-L6 honest-verify scorecard → register) ──────


class LayerStatus(enum.Enum):
    """Outcome of one adoption layer — every value earned from a real probe."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class LayerResult:
    """One scorecard row: the layer, what the wire said, and the verdict."""

    layer: str
    label: str
    status: LayerStatus
    detail: str


#: pmset values the bootstrap script sets; L5 verifies them from a real read.
_HEADLESS_EXPECTED: tuple[tuple[str, str], ...] = (
    ("sleep", "0"),
    ("disksleep", "0"),
    ("powernap", "0"),
    ("womp", "1"),
)

_PMSET_LINE_RE = re.compile(r"^\s*(\w+)\s+(\S+)\s*$", re.MULTILINE)


def parse_pmset_custom(text: str) -> dict[str, str]:
    """Parse ``pmset -g custom`` output into a flat ``{setting: value}`` map.

    Section headers ("AC Power:") don't match the setting-line shape and drop
    out; on a two-section laptop the last section wins, which is exactly right —
    the bootstrap's ``pmset -a`` writes both, so a divergence still surfaces.
    """
    return {m.group(1): m.group(2) for m in _PMSET_LINE_RE.finditer(text)}


def _probe_auth(host: str, user: str) -> LayerResult:
    """L2: the automation key actually authenticates as ``user``."""
    rc, out = _run_ssh(host, user, "whoami")
    who = out.strip()
    if rc == 0 and who == user:
        return LayerResult("L2", "ssh auth", LayerStatus.PASS, f"automation key accepted ({user})")
    if rc == 0:
        return LayerResult("L2", "ssh auth", LayerStatus.FAIL, f"landed as {who or '?'}, wanted {user}")
    return LayerResult(
        "L2", "ssh auth", LayerStatus.FAIL, f"ssh failed (rc {rc}) — did the bootstrap script run?"
    )


def _probe_identity(host: str, user: str) -> tuple[LayerResult, str]:
    """L3: read who the machine is — (result, short hostname for the node name)."""
    rc, out = _run_ssh(host, user, 'printf "%s|%s|%s" "$(hostname -s)" "$(sw_vers -productVersion)" "$(uname -m)"')
    parts = out.strip().split("|")
    if rc == 0 and len(parts) == 3 and all(parts):
        hostname, macos, arch = parts
        return (
            LayerResult("L3", "identity", LayerStatus.PASS, f"{hostname} · macOS {macos} · {arch}"),
            hostname,
        )
    return (LayerResult("L3", "identity", LayerStatus.FAIL, "could not read hostname/os/arch"), "")


def _probe_key_hygiene(host: str, user: str, pubkey: str) -> LayerResult:
    """L4: authorized_keys perms + our key actually present (not just "auth worked").

    Greps for the key's base64 blob (field 2 — no shell metacharacters by
    construction), so a satellite that let us in via some OTHER identity still
    fails honestly here.
    """
    parts = pubkey.split()
    blob = parts[1] if len(parts) >= 2 else pubkey
    cmd = (
        'stat -f%Lp "$HOME/.ssh"; stat -f%Lp "$HOME/.ssh/authorized_keys"; '
        f"grep -qF '{blob}' \"$HOME/.ssh/authorized_keys\" && echo key-present || echo key-absent"
    )
    rc, out = _run_ssh(host, user, cmd)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if rc != 0 or len(lines) < 3:
        return LayerResult("L4", "key hygiene", LayerStatus.FAIL, "could not read ~/.ssh state")
    dir_perm, file_perm, presence = lines[0], lines[1], lines[2]
    problems: list[str] = []
    if dir_perm != "700":
        problems.append(f"~/.ssh is {dir_perm}, want 700")
    if file_perm != "600":
        problems.append(f"authorized_keys is {file_perm}, want 600")
    if presence != "key-present":
        problems.append("automation key not in authorized_keys")
    if problems:
        return LayerResult("L4", "key hygiene", LayerStatus.FAIL, "; ".join(problems))
    return LayerResult("L4", "key hygiene", LayerStatus.PASS, "700/600 + automation key present")


def _probe_headless(host: str, user: str) -> LayerResult:
    """L5: headless-reliability posture, read back from the machine itself.

    ``pmset -g custom`` needs no root; restart-on-power-failure does, so it is
    verified only when ``sudo -n`` works (the ``--sudo-nopasswd`` path) and
    reported unverified otherwise — never guessed. Divergent pmset values WARN
    rather than FAIL: the node is adoptable, just not closet-hardened yet, and
    the scorecard says so with the fix.
    """
    rc, out = _run_ssh(host, user, "pmset -g custom")
    if rc != 0:
        return LayerResult("L5", "headless", LayerStatus.WARN, "pmset unreadable — posture unknown")
    values = parse_pmset_custom(out)
    off: list[str] = []
    for key, want in _HEADLESS_EXPECTED:
        got = values.get(key)
        if got is None:
            if key != "powernap":  # some hardware has no Power Nap at all
                off.append(f"{key} unreported")
        elif got != want:
            off.append(f"{key}={got} (want {want})")

    sudo_rc, _ = _run_ssh(host, user, "sudo -n true")
    if sudo_rc == 0:
        rpf_rc, rpf_out = _run_ssh(host, user, "sudo -n systemsetup -getrestartpowerfailure")
        if rpf_rc != 0 or "on" not in rpf_out.lower():
            off.append("restart-on-power-failure not on")
        rpf_note = ""
    else:
        rpf_note = " · restart-on-power-failure unverified (no passwordless sudo)"

    if off:
        return LayerResult(
            "L5",
            "headless",
            LayerStatus.WARN,
            "; ".join(off) + rpf_note + " — re-run the bootstrap script's step 3",
        )
    return LayerResult("L5", "headless", LayerStatus.PASS, "sleep/disksleep/powernap 0 · womp 1" + rpf_note)


def register_node(name: str, *, host: str, user: str, path: Path | None = None) -> None:
    """Record an adopted satellite under ``nodes.<name>`` in instance.yaml.

    Raw read-modify-write (matching ``onboard.set_instance_identity``): every
    other block is preserved, a ``<file>.bak`` is written first, and the parent
    dir is created for a fresh file. ``nodes:`` is deliberately NOT modeled in
    :class:`config.Config` — it is read back via ``config.instance_value``, the
    per-setup-block convention.
    """
    target = Path(path) if path else config.instance_path()
    data: dict[str, Any] = {}
    if target.exists():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        nodes = data["nodes"] = {}
    nodes[name] = {"host": host, "user": user, "adopted": date.today().isoformat()}
    if target.exists():
        backup = target.parent / (target.name + ".bak")
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


_LAYER_STYLE = {
    LayerStatus.PASS: ("green", "✓"),
    LayerStatus.WARN: ("yellow", "!"),
    LayerStatus.FAIL: ("red", "✗"),
    LayerStatus.SKIP: ("dim", "·"),
}


def _render_scorecard(rows: list[LayerResult]) -> None:
    """Render the L1-L6 honest-verify scorecard."""
    table = Table(title="Adoption scorecard", title_justify="left")
    table.add_column("", no_wrap=True)
    table.add_column("layer", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail", overflow="fold")
    for r in rows:
        style, glyph = _LAYER_STYLE[r.status]
        table.add_row(
            f"[{style}]{glyph}[/]",
            f"[bold]{r.layer} {escape(r.label)}[/]",
            f"[{style}]{r.status.value}[/]",
            f"[dim]{escape(r.detail)}[/]",
        )
    console.print(table)


@node_app.command("adopt", help="Adopt a bootstrapped Mac: poll SSH, verify L1-L6, register it.")
def node_adopt(
    host: Annotated[
        str, typer.Argument(help="IP or hostname of the Mac to adopt (see `sanctum node scan`).")
    ],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Node name to register under (default: its hostname, slugified)."),
    ] = None,
    user: Annotated[
        str | None,
        typer.Option("--user", help="Account on the satellite (default: same as this console)."),
    ] = None,
    wait: Annotated[
        int,
        typer.Option("--wait", min=0, help="Seconds to poll for SSH before giving up (0 = one probe)."),
    ] = 90,
) -> None:
    """The console side of the bootstrap hand-off — an honest-verify scorecard.

    Polls :22 until the bootstrap script (running at the satellite's keyboard)
    opens the door, then drives every layer as a REAL probe over the wire:

    * L1 reach — TCP :22 answers
    * L2 ssh auth — the automation key logs in as the expected user
    * L3 identity — hostname / macOS version / arch read back
    * L4 key hygiene — 700/600 perms + OUR key in authorized_keys
    * L5 headless — pmset posture (WARN-only: adoptable but not closet-ready)
    * L6 register — ``nodes.<name>`` written to instance.yaml and READ BACK

    L1-L4 are required: any FAIL skips registration and exits non-zero with the
    scorecard as the diagnosis. A ✓ that wasn't earned from the wire does not
    exist here (honest-verify doctrine).
    """
    ssh_user = user or getpass.getuser()
    key = _automation_key_path()
    if not key.exists() or not key.with_suffix(".pub").exists():
        exc = LocalError(
            f"automation key pair not found at {key}",
            fix="run `sanctum node bootstrap-script` first — it mints the pair",
        )
        _report(exc)
        raise typer.Exit(code=int(exc.exit_code))
    pubkey = _ensure_automation_pubkey()

    # L1: poll until the bootstrap opens :22 (or the wait budget runs out).
    deadline = time.monotonic() + wait
    reachable = _probe_tcp(host, _SSH_PORT)
    while not reachable and time.monotonic() < deadline:
        time.sleep(_ADOPT_POLL_INTERVAL_S)
        reachable = _probe_tcp(host, _SSH_PORT)
    if not reachable:
        net_exc = NetworkError(
            f"{host}:22 never answered — the satellite still needs its bootstrap",
            fix="run the `sanctum node bootstrap-script` output at that Mac's keyboard, "
            "then re-run adopt",
        )
        _report(net_exc)
        raise typer.Exit(code=int(net_exc.exit_code))
    rows: list[LayerResult] = [
        LayerResult("L1", "reach", LayerStatus.PASS, f"{host}:22 answers")
    ]

    # L2-L5: real remote probes. L3's hostname doubles as the default node name.
    auth = _probe_auth(host, ssh_user)
    rows.append(auth)
    if auth.status is LayerStatus.PASS:
        identity, remote_hostname = _probe_identity(host, ssh_user)
        rows.append(identity)
        rows.append(_probe_key_hygiene(host, ssh_user, pubkey))
        rows.append(_probe_headless(host, ssh_user))
    else:
        remote_hostname = ""
        rows.append(LayerResult("L3", "identity", LayerStatus.SKIP, "blocked by L2"))
        rows.append(LayerResult("L4", "key hygiene", LayerStatus.SKIP, "blocked by L2"))
        rows.append(LayerResult("L5", "headless", LayerStatus.SKIP, "blocked by L2"))

    required_ok = all(r.status is LayerStatus.PASS for r in rows if r.layer in ("L1", "L2", "L3", "L4"))

    # L6: register only over a proven L1-L4, and trust only the read-back.
    if required_ok:
        node_name = name or config.slugify_name(remote_hostname)
        try:
            register_node(node_name, host=host, user=ssh_user)
            written = config.instance_value(f"nodes.{node_name}.host") == host
        except OSError:
            written = False
        if written:
            rows.append(
                LayerResult(
                    "L6", "register", LayerStatus.PASS, f"nodes.{node_name} → {host} (read back)"
                )
            )
        else:
            rows.append(
                LayerResult("L6", "register", LayerStatus.FAIL, "instance.yaml write did not read back")
            )
    else:
        rows.append(LayerResult("L6", "register", LayerStatus.SKIP, "blocked by a failing layer"))

    _render_scorecard(rows)

    if not required_ok or rows[-1].status is not LayerStatus.PASS:
        console.print("[red]NOT ADOPTED[/] — fix the failing layers above and re-run.")
        raise typer.Exit(code=1)
    warned = any(r.status is LayerStatus.WARN for r in rows)
    if warned:
        console.print(
            f"[green]ADOPTED[/] [yellow](with warnings)[/] — "
            f"{escape(host)} is registered; see the WARN rows above."
        )
    else:
        console.print(f"[green]ADOPTED[/] — {escape(host)} is registered and closet-ready.")
