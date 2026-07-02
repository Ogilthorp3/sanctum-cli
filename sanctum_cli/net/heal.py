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
from pathlib import Path

from sanctum_cli.net.detect import lan_conflicts_with_bell_dmz, parse_default_gateway
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

# no-loop guard: after this many heal attempts (persisted across daemon runs) we
# stop and alert a human rather than re-flapping the interface forever.
MAX_HEAL_ATTEMPTS = 3

# Machine-readable result marker `net heal --apply` emits on its own so the daemon
# wrapper detects success on an UNAMBIGUOUS token — NOT on human prose. The failure
# path prints "✗ not healed …", which CONTAINS the substring "healed"; a naive
# `grep -q 'healed'` matched BOTH lines and reset the no-loop counter on every
# reverted heal → the daemon re-fired the same failing heal forever (toggle-storm).
# The wrapper now anchors on the exact `HEAL_RESULT_MARKER=healed` token, and the
# CLI derives the token from the SAME real re-probe the ✓ derives from. The three
# tokens are mutually exclusive: `healed` (re-probe passed: lease + reachable
# gateway), `reverted` (fired but stayed unhealthy → reverted), `noop` (dry-run /
# stop-and-alert / nothing to do / non-root). Any non-`healed` outcome (or no token
# at all) must INCREMENT the attempts counter so the cap accrues.
HEAL_RESULT_MARKER = "NET_HEAL_RESULT"
HEAL_RESULT_HEALED = f"{HEAL_RESULT_MARKER}=healed"
HEAL_RESULT_REVERTED = f"{HEAL_RESULT_MARKER}=reverted"
HEAL_RESULT_NOOP = f"{HEAL_RESULT_MARKER}=noop"


@dataclass(frozen=True)
class HealAction:
    """A single remediation the diagnosis prescribes.

    ``kind`` is one of ``none|flip_dhcp|dhcp_renew|alert_only``. ``safe`` is the
    load-bearing guard: only a ``safe`` action is ever a candidate for mutation
    (:func:`plan_heal` still gates it on the spine + attempts cap). An
    ``alert_only`` action is *never* safe — it is the fail-closed / stays-out-of-
    the-NAT-domain verdict where the node stops and hands off to a human.
    """

    kind: str
    safe: bool
    detail: str


@dataclass(frozen=True)
class PostureDiagnosis:
    """The classified verdict over a :class:`NetPosture` (pure truth-table output).

    ``verdict`` is one of ``HEALTHY|STATIC_DRIFT|GATEWAY_DEAD|WRONG_SUBNET|
    DOUBLE_NAT_OVERLAP|UNVERIFIED``. ``remedy`` is the one-line human fix (shown
    when the plan is stop-and-alert). ``action`` carries the machine remedy +
    its safety. ``posture`` is the input, retained so callers can render / re-
    probe against it.
    """

    verdict: str
    detail: str
    remedy: str
    action: HealAction
    posture: NetPosture


@dataclass(frozen=True)
class HealPlan:
    """The guarded decision over a :class:`PostureDiagnosis` (pure output).

    ``execute`` is the single load-bearing gate: it is ``True`` only when every
    doctrine gate passes — the diagnosed ``action`` is ``safe``, the no-loop
    attempts cap is not yet reached, and the never-strand spine (tailnet / TB5)
    is alive. ``action`` is the action to run (``None`` when we stop). ``reason``
    is the human-readable *why* — carrying the specific stop cause (alert-only /
    attempts-exhausted / spine-down) so the CLI / daemon can surface it verbatim.
    """

    execute: bool
    action: HealAction | None
    reason: str


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


# ─── diagnosis (pure truth table) ──────────────────────────────────────


def _gateway_off_subnet(posture: NetPosture) -> bool:
    """True iff the default gateway is *outside* the IP's own subnet.

    A gateway that does not sit inside our IP/mask network is unroutable — the
    signature of a stale static lease left over from a renumber (WRONG_SUBNET).
    Fail-closed: any missing / unparseable field returns False (we do not invent
    a wrong-subnet verdict from an unread posture).
    """
    if not (posture.ip and posture.subnet and posture.gateway):
        return False
    try:
        net = ipaddress.ip_network(f"{posture.ip}/{posture.subnet}", strict=False)
        gw = ipaddress.ip_address(posture.gateway)
    except ValueError:
        return False
    return gw not in net


def diagnose_posture(
    posture: NetPosture, *, overlap: bool = False
) -> PostureDiagnosis:
    """Classify a :class:`NetPosture` into a verdict + a guarded remedy (pure).

    The truth table, in priority order:

    * **UNVERIFIED** — no iface / no ConfigMethod: the posture could not be read.
      Fail-closed: ``alert_only`` (never a mutating action from an unread state).
    * **DOUBLE_NAT_OVERLAP** — the caller detected the LAN overlaps Bell's
      Advanced-DMZ WAN (``net.detect.lan_conflicts_with_bell_dmz``) *and* the
      gateway is unreachable. Stays out of the NAT domain: ``alert_only`` (the
      fix is a router/NAT change a human must make — we never touch it).
    * **STATIC_DRIFT** — ConfigMethod is ``Manual``: a pinned static address that
      strands the node on any foreign LAN. Auto-heal to DHCP (``flip_dhcp``).
    * **GATEWAY_DEAD** — associated with a live link but the default gateway does
      not answer: a stale lease / gateway change. Guarded ``dhcp_renew``.
    * **WRONG_SUBNET** — the gateway sits outside our IP's subnet (renumber):
      guarded ``dhcp_renew``.
    * **HEALTHY** — none of the above.
    """
    # Fail-closed first: an unreadable posture never yields a mutating action.
    if not posture.iface or not posture.config_method:
        return PostureDiagnosis(
            verdict="UNVERIFIED",
            detail="Could not read the network posture (no interface / ConfigMethod).",
            remedy="Re-run once the Wi-Fi interface is up; do not mutate an unread posture.",
            action=HealAction("alert_only", safe=False, detail="posture unread — fail closed"),
            posture=posture,
        )

    # Stays-out-of-the-NAT-domain: an overlapping-DMZ LAN with a dead gateway is a
    # router/NAT problem — alert a human, never touch the NAT ourselves.
    if overlap and posture.gateway_reachable is False:
        return PostureDiagnosis(
            verdict="DOUBLE_NAT_OVERLAP",
            detail="LAN overlaps Bell's Advanced-DMZ WAN range and the gateway is dead.",
            remedy="Renumber the LAN off 0.x-127.x (change the router's subnet); we stay out of the NAT domain.",
            action=HealAction("alert_only", safe=False, detail="NAT-domain change — human only"),
            posture=posture,
        )

    # STATIC_DRIFT takes priority over reachability: a Manual address is the root
    # strand risk regardless of whether this particular gateway happens to answer.
    if posture.config_method == "Manual":
        return PostureDiagnosis(
            verdict="STATIC_DRIFT",
            detail="Interface is on a Manual (static) address — strands the node on any foreign LAN.",
            remedy="Flip Wi-Fi to DHCP (networksetup -setdhcp \"Wi-Fi\").",
            action=HealAction("flip_dhcp", safe=True, detail="Manual → DHCP"),
            posture=posture,
        )

    if posture.associated and posture.gateway_reachable is False:
        return PostureDiagnosis(
            verdict="GATEWAY_DEAD",
            detail="Associated to a live link but the default gateway does not answer.",
            remedy="Renew the DHCP lease (ipconfig set <iface> DHCP).",
            action=HealAction("dhcp_renew", safe=True, detail="stale lease — renew"),
            posture=posture,
        )

    if _gateway_off_subnet(posture):
        return PostureDiagnosis(
            verdict="WRONG_SUBNET",
            detail="Default gateway is outside the IP's own subnet — stale lease after a renumber.",
            remedy="Renew the DHCP lease (ipconfig set <iface> DHCP).",
            action=HealAction("dhcp_renew", safe=True, detail="renumber — renew"),
            posture=posture,
        )

    return PostureDiagnosis(
        verdict="HEALTHY",
        detail="Posture is healthy — DHCP, reachable gateway, on-subnet.",
        remedy="",
        action=HealAction("none", safe=True, detail="no action"),
        posture=posture,
    )


# ─── plan (pure guard — never-strand + no-loop + fail-closed) ──────────


def plan_heal(
    diagnosis: PostureDiagnosis,
    *,
    attempts: int,
    tailnet_ok: bool,
    tb5_ok: bool = False,
) -> HealPlan:
    """Guard a diagnosis into an executable (or stop-and-alert) plan (pure).

    Encodes the three self-heal doctrines as ordered hard gates; a mutation is
    planned ONLY when *all three* pass:

    * **stays-out-of-the-NAT-domain / fail-closed** — the diagnosed ``action``
      must be ``safe``. An ``alert_only`` verdict (DOUBLE_NAT_OVERLAP, UNVERIFIED)
      is never executed: we stop and hand the one-line remedy to a human.
    * **no-loop** — ``attempts`` (persisted across daemon runs) must be below
      :data:`MAX_HEAL_ATTEMPTS`; at the cap we stop and alert rather than re-flap
      the interface forever.
    * **never-strand** — at least one spine (``tailnet_ok`` or ``tb5_ok``) must be
      alive, so a failed heal + auto-revert is always reachable out-of-band. With
      no spine we do NOT touch the interface (a bad flip could strand the node).

    The stop ``reason`` is specific per gate so callers surface the real cause.
    """
    action = diagnosis.action
    if action is None or not action.safe:
        return HealPlan(
            execute=False,
            action=None,
            reason=f"stop-and-alert: {diagnosis.verdict} is not auto-healable ({action.detail if action else 'no action'}).",
        )
    if attempts >= MAX_HEAL_ATTEMPTS:
        return HealPlan(
            execute=False,
            action=None,
            reason=f"stop-and-alert: heal attempts exhausted ({attempts}/{MAX_HEAL_ATTEMPTS}) — no-loop guard.",
        )
    if not (tailnet_ok or tb5_ok):
        return HealPlan(
            execute=False,
            action=None,
            reason="stop-and-alert: never-strand spine down (no tailnet and no TB5) — refusing to mutate the interface.",
        )
    return HealPlan(
        execute=True,
        action=action,
        reason=f"heal: {diagnosis.verdict} → {action.kind} (spine up, attempt {attempts + 1}/{MAX_HEAL_ATTEMPTS}).",
    )


# ─── CLI-facing pure helpers (posture → overlap / action argv) ─────────


def posture_cidr(posture: NetPosture) -> str:
    """The node's own network as a CIDR (e.g. ``10.0.0.10/255.255.255.0``), or "".

    Feeds :func:`overlap_for`. Fail-closed: any missing / unparseable field yields
    "" (which :func:`overlap_for` treats as no overlap), so we never invent a
    double-NAT overlap from an unread posture.
    """
    if not (posture.ip and posture.subnet):
        return ""
    try:
        net = ipaddress.ip_network(f"{posture.ip}/{posture.subnet}", strict=False)
    except ValueError:
        return ""
    return str(net)


def overlap_for(posture: NetPosture) -> bool:
    """True iff the node's own LAN overlaps Bell's Advanced-DMZ WAN range.

    Pure wrapper over :func:`net.detect.lan_conflicts_with_bell_dmz` — the caller
    passes the result to :func:`diagnose_posture` as ``overlap`` so a double-NAT
    overlap is classified ``alert_only`` (stays out of the NAT domain). An
    unreadable posture (empty CIDR) is never an overlap (no false alarm).
    """
    cidr = posture_cidr(posture)
    return bool(cidr) and lan_conflicts_with_bell_dmz(cidr)


def heal_action_argv(action: HealAction, iface: str) -> list[str]:
    """The concrete argv a ``safe`` :class:`HealAction` runs (empty for none/alert).

    * ``flip_dhcp`` → ``networksetup -setdhcp "Wi-Fi"`` (Manual → DHCP, the
      DHCP-not-static heal).
    * ``dhcp_renew`` → ``ipconfig set <iface> DHCP`` (bounce the lease).

    The port label is the stable macOS ``"Wi-Fi"`` service name for the flip;
    ``dhcp_renew`` operates on the resolved BSD ``iface``. A non-mutating action
    (``none`` / ``alert_only``) returns ``[]`` — the caller must never shell out
    for it (fail-closed at the boundary too).
    """
    if action.kind == "flip_dhcp":
        return ["networksetup", "-setdhcp", "Wi-Fi"]
    if action.kind == "dhcp_renew":
        return ["ipconfig", "set", iface, "DHCP"]
    return []


# ─── self-healing daemon assets (shipped by `sanctum net heal --install`) ──
#
# Unlike the wifi-stability *sentinel* (a per-user LaunchAgent that only samples),
# the net-heal daemon must mutate the interface (setdhcp / renew), which needs
# root — so it is a system LaunchDaemon under /Library/LaunchDaemons, installed
# with the one sudo step (`launchctl bootstrap system`). It runs the real CLI
# (`sanctum net heal --apply`) on a bounded cadence behind three doctrine gates
# already encoded in the pure core: never-strand (plan_heal's spine gate), no-loop
# (MAX_HEAL_ATTEMPTS, persisted here across runs), and a DISABLED kill-switch the
# wrapper honors before doing anything.

HEAL_DAEMON_LABEL = "com.sanctum.net-heal"
HEAL_INTERVAL_S = 120

# State the wrapper persists so the no-loop cap survives across daemon runs.
_HEAL_STATE_DIR = Path("/Library/Application Support/sanctum")
_HEAL_ATTEMPTS_FILE = _HEAL_STATE_DIR / "net-heal.attempts"
_HEAL_HEARTBEAT_FILE = _HEAL_STATE_DIR / "net-heal.heartbeat"
# The kill-switch: `touch` this and the daemon no-ops every cycle (fail-safe off).
_HEAL_DISABLED_SENTINEL = _HEAL_STATE_DIR / "net-heal.DISABLED"

# The wrapper the LaunchDaemon executes. Bash (parsed by `bash -n` in tests). It:
#  1. honors the DISABLED kill-switch first (no-op + heartbeat, never mutate),
#  2. enforces the no-loop cap (MAX_HEAL_ATTEMPTS attempts persisted on disk; at
#     the cap it stops auto-healing and alerts rather than re-flapping forever),
#  3. runs the real `sanctum net heal --apply` (which itself re-checks the spine +
#     snapshot→verify→auto-reverts — the wrapper never touches the interface
#     directly), incrementing the attempt counter on a non-healed exit,
#  4. verifies the never-strand spine (tailnet 100.64/10 or TB5 10.0.5.x) each
#     cycle and alerts (best-effort, degrade) when it is down — the "about to be
#     strandable" page,
#  5. always writes a heartbeat so a stalled daemon is observable.
# Absolute paths only (launchd does not expand ~, and root has a bare env).
HEAL_WRAPPER = f"""#!/bin/bash
# Sanctum net-heal daemon wrapper — runs `sanctum net heal --apply` behind the
# DISABLED kill-switch + a persisted no-loop attempts cap, verifies the
# never-strand spine, and writes a heartbeat. Runs as root (LaunchDaemon) so the
# CLI can flip DHCP / renew the lease. Absolute paths only.
set -u
STATE_DIR="{_HEAL_STATE_DIR}"
ATTEMPTS_FILE="{_HEAL_ATTEMPTS_FILE}"
HEARTBEAT_FILE="{_HEAL_HEARTBEAT_FILE}"
DISABLED="{_HEAL_DISABLED_SENTINEL}"
MAX_ATTEMPTS={MAX_HEAL_ATTEMPTS}
umask 077
mkdir -p "$STATE_DIR"

now() {{ date '+%Y-%m-%dT%H:%M:%S'; }}
heartbeat() {{ printf '%s %s\\n' "$(now)" "$1" >> "$HEARTBEAT_FILE"; }}

# Resolve the sanctum CLI (bare root env has no user PATH additions).
SANCTUM="$(command -v sanctum || true)"
[ -z "$SANCTUM" ] && [ -x /opt/homebrew/bin/sanctum ] && SANCTUM=/opt/homebrew/bin/sanctum
[ -z "$SANCTUM" ] && [ -x /usr/local/bin/sanctum ] && SANCTUM=/usr/local/bin/sanctum
if [ -z "$SANCTUM" ]; then
  heartbeat "ERROR sanctum-cli-not-found"
  exit 0
fi

# 1. Kill-switch: DISABLED sentinel present → no-op (fail-safe off), never mutate.
if [ -e "$DISABLED" ]; then
  heartbeat "DISABLED kill-switch present — no-op"
  exit 0
fi

# 2. No-loop cap: at MAX_ATTEMPTS we stop auto-healing + alert (no toggle-storm).
ATTEMPTS=0
[ -f "$ATTEMPTS_FILE" ] && ATTEMPTS="$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo 0)"
case "$ATTEMPTS" in ''|*[!0-9]*) ATTEMPTS=0 ;; esac
if [ "$ATTEMPTS" -ge "$MAX_ATTEMPTS" ]; then
  heartbeat "STOP attempts=$ATTEMPTS/$MAX_ATTEMPTS — no-loop cap; auto-heal paused"
  exit 0
fi

# 3. Never-strand spine check (read-only): tailnet 100.64/10 or TB5 10.0.5.x up?
SPINE="down"
if ifconfig 2>/dev/null | grep -Eq 'inet (100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.|10\\.0\\.5\\.)'; then
  SPINE="up"
else
  heartbeat "ALERT spine down (no tailnet / no TB5) — node is about to be strandable"
fi

# 4. Run the real CLI heal (it re-checks spine + snapshot→verify→auto-reverts).
#    Success is detected on the EXACT machine-readable token the CLI emits from
#    its real re-probe (`{HEAL_RESULT_HEALED}`), NOT on the word "healed" — the
#    failure/revert line ("✗ not healed …") also contains "healed", so a substring
#    match would falsely reset the no-loop counter on every reverted heal and the
#    daemon would re-fire the failing heal forever (the toggle-storm this cap
#    exists to prevent). Anchoring on the token means a `reverted`/`noop` outcome
#    (or no token at all) falls through to the else branch and INCREMENTS attempts.
OUT="$("$SANCTUM" net heal --apply 2>&1)"
RC=$?
if printf '%s' "$OUT" | grep -q '{HEAL_RESULT_HEALED}'; then
  # A real re-probe verified the heal — reset the attempts counter.
  echo 0 > "$ATTEMPTS_FILE"
  heartbeat "healed spine=$SPINE rc=$RC"
else
  # Not healed this cycle (reverted / noop / no token) — bump the persisted
  # attempts (no-loop backoff) so the MAX_ATTEMPTS cap accrues and stops+alerts.
  ATTEMPTS=$((ATTEMPTS + 1))
  echo "$ATTEMPTS" > "$ATTEMPTS_FILE"
  heartbeat "no-heal attempts=$ATTEMPTS/$MAX_ATTEMPTS spine=$SPINE rc=$RC"
fi
exit 0
"""

# The LaunchDaemon template. launchd does NOT expand ``~``, so the wrapper path +
# error-log path MUST be absolute — :func:`render_heal_plist` fills them. Mirrors
# ``link.SENTINEL_PLIST`` but is a *system* daemon (no per-user domain) so it can
# mutate the interface as root.
HEAL_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{wrapper}</string>
  </array>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>{err_log}</string>
</dict>
</plist>
"""


def heal_wrapper_path() -> Path:
    """Where ``--install`` writes the daemon wrapper (0755, root-owned)."""
    return _HEAL_STATE_DIR / "net-heal.sh"


def heal_plist_path() -> Path:
    """Where ``--install`` writes the LaunchDaemon plist (system domain)."""
    return Path("/Library/LaunchDaemons") / f"{HEAL_DAEMON_LABEL}.plist"


def heal_err_path() -> Path:
    """The plist's StandardErrorPath for the daemon."""
    return _HEAL_STATE_DIR / "net-heal.err"


def render_heal_plist(
    *, wrapper: Path | None = None, err_log: Path | None = None
) -> str:
    """Render :data:`HEAL_PLIST` with absolute paths.

    Defaults resolve from the path helpers so the caller normally renders with no
    arguments; tests inject ``wrapper`` / ``err_log`` to write into a temp dir.
    """
    wrapper = wrapper if wrapper is not None else heal_wrapper_path()
    err_log = err_log if err_log is not None else heal_err_path()
    return HEAL_PLIST.format(
        label=HEAL_DAEMON_LABEL,
        wrapper=wrapper,
        interval=HEAL_INTERVAL_S,
        err_log=err_log,
    )
