"""Sanctum Link diagnose — the pure half of the Link Optimizer (Measure + Diagnose).

Classify a node's link instability from the stability-sentinel log into
``RADIO`` / ``LOAD`` / ``SCAN`` / ``HEALTHY`` / ``NO_DATA``, each with a targeted
remedy. Universal: every Sanctum node that runs the sentinel (``rtt`` + ``load``
samples) can run this. Pure stdlib; :func:`classify` is a pure function over
parsed samples, so it is fully unit-testable without a network.

This module also carries the shippable sentinel assets — :data:`SENTINEL_SCRIPT`
(the bash sampler) and :data:`SENTINEL_PLIST` (the LaunchAgent template) — plus
the path helpers ``sanctum link install`` uses, so install writes them with NO
external files. The sampler resolves the default interface + first LAN hop at
runtime (no hardcoded haus IP/iface), so the same bytes work on any user's LAN.
"""

from __future__ import annotations

import math
import plistlib
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# ─── parser ──────────────────────────────────────────────────────────

# The rtt block is OPTIONAL: on 100% packet loss macOS `ping` prints no
# round-trip line, so the sampler emits `rtt=NA loss=100.0% ...` (see
# SENTINEL_SCRIPT). The loss field is therefore parsed INDEPENDENTLY of rtt —
# capturing loss only when rtt is numeric would silently drop every total-loss
# window, discarding exactly the highest-loss (dead-radio) evidence. Both rtt
# and loss tolerate the literal token `NA`.
_LINE = re.compile(
    r"rtt=(?:NA|(?P<min>[\d.]+)/(?P<avg>[\d.]+)/(?P<max>[\d.]+)/(?P<std>[\d.]+))"
    r"\s+loss=(?P<loss>[\d.]+|NA)%"
    r"\s+load=\[(?P<load>[\d.]+)"
)

# Lines the sampler tags when it found NO first-LAN-hop gateway: it deliberately
# does NOT ping (an external-host ping would measure WAN, not the radio), so
# these carry no link signal and are excluded from the diagnosis.
_NO_GATEWAY_TAG = "NO_GATEWAY"

# A total-loss window has no measurable rtt. We synthesize this high sentinel so
# the dead window reads as the worst latency in the metrics and never as fast;
# its 100% loss is what actually drives the RADIO verdict.
_DEAD_RTT_MS = 9999.0

# ─── thresholds (tunable; chosen from the reference Mini dataset) ────

# DELIBERATE divergence from a literal "RADIO when loss > 0": sub-1% mean loss is
# treated as a noise floor (a single stray ICMP drop in a healthy window must not
# scream RADIO). >1% mean loss is the intended, explicit RADIO definition — a
# real lossy/dead radio (e.g. one 100%-loss window in a small sample) clears it.
RADIO_LOSS_PCT = 1.0  # >1% mean loss => the radio itself is dropping frames
DEGRADED_FRAC = 0.20  # below this fraction degraded => HEALTHY
LOAD_PEARSON = 0.50  # latency<->load correlation that calls it LOAD-bound
LOAD_RATIO = 1.30  # degraded-window mean load this much above ok windows

# status() classifies only the most recent slice — a node that was LOAD-bound
# months ago must not drag the "is my link OK now?" verdict. ~24h at 3-min cadence.
STATUS_WINDOW_SAMPLES = 480


@dataclass(frozen=True)
class Sample:
    """One parsed sentinel sample (a single ping window + the node's load)."""

    min: float
    avg: float
    max: float
    std: float
    loss: float
    load: float
    degraded: bool


@dataclass(frozen=True)
class Metrics:
    """Summary statistics over the analysed sample window."""

    samples: int
    degraded_pct: float
    mean_loss_pct: float
    p50_avg_ms: float
    worst_avg_ms: float


@dataclass(frozen=True)
class Diagnosis:
    """The classifier verdict + human detail + targeted remedy + metrics."""

    verdict: str
    detail: str
    remedy: str
    metrics: Metrics | None


def parse_log(text: str) -> list[Sample]:
    """Parse sentinel log lines into samples. Unparseable lines are skipped.

    The safety-critical case is a total-loss / dead-link window: on 100% packet
    loss macOS `ping` prints no round-trip line, so the sampler emits
    ``... rtt=NA loss=100.0% load=[...] DEGRADED``. That line carries NO numeric
    rtt, so loss MUST be read on its own channel or the window vanishes — taking
    the worst evidence with it (a mixed good+dead window would then read HEALTHY,
    a pure dead window NO_DATA). For an ``rtt=NA`` window we synthesize a Sample
    with a high sentinel latency and ``degraded=True`` so it feeds the RADIO
    mean-loss path; unknown loss (``loss=NA``) is treated as 100% — fail-safe.

    ``NO_GATEWAY`` lines (no first-LAN-hop gateway was found, so the sampler did
    not ping at all) carry no link signal and are excluded, so a missing gateway
    is never misdiagnosed as a RADIO/LOAD problem.
    """
    out: list[Sample] = []
    for ln in text.splitlines():
        m = _LINE.search(ln)
        if not m:
            continue
        if _NO_GATEWAY_TAG in ln:
            continue
        loss_raw = m.group("loss")
        if m.group("avg") is None:
            # rtt=NA → ping returned no round-trip line: total loss / dead link.
            out.append(
                Sample(
                    min=_DEAD_RTT_MS,
                    avg=_DEAD_RTT_MS,
                    max=_DEAD_RTT_MS,
                    std=0.0,
                    loss=float(loss_raw) if loss_raw != "NA" else 100.0,
                    load=float(m.group("load")),
                    degraded=True,
                )
            )
            continue
        out.append(
            Sample(
                min=float(m.group("min")),
                avg=float(m.group("avg")),
                max=float(m.group("max")),
                std=float(m.group("std")),
                loss=float(loss_raw) if loss_raw != "NA" else 0.0,
                load=float(m.group("load")),
                degraded=ln.rstrip().endswith("DEGRADED"),
            )
        )
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation of two equal-length series; 0.0 when undefined."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = float(sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)))
    vx = float(sum((x - mx) ** 2 for x in xs))
    vy = float(sum((y - my) ** 2 for y in ys))
    if vx == 0.0 or vy == 0.0:
        return 0.0
    return cov / math.sqrt(vx * vy)


_REMEDY: dict[str, str] = {
    "NO_DATA": "Let the sentinel collect samples (runs every ~3 min), then re-run.",
    "HEALTHY": "Link is stable — no action. Keep the sentinel watching.",
    "RADIO": (
        "Radio is dropping frames: check RSSI/interference, move the node or "
        "AP, set a clean fixed channel, or prefer a wired uplink."
    ),
    "LOAD": (
        "Latency tracks this node's own CPU/traffic load (self-bufferbloat) — "
        "the radio is fine. Wi-Fi tuning has limited headroom here; the real "
        "fix is a WIRED uplink (Ethernet > MoCA > powerline) with Wi-Fi "
        "failover. Interim: egress fq_codel/SQM + max channel width."
    ),
    "SCAN": (
        "Periodic off-channel spikes not tied to load — background scanning / "
        "roaming. Prune remembered networks to the one in use, disable "
        "auto-join of others, disable power-save."
    ),
}


def classify(samples: list[Sample]) -> Diagnosis:
    """Pure classifier over parsed sentinel samples.

    RADIO when the radio is lossy; HEALTHY when few windows degraded; otherwise
    LOAD when latency tracks the node's own load, else SCAN (off-channel spikes
    uncorrelated with load).
    """
    n = len(samples)
    if n == 0:
        return Diagnosis(
            verdict="NO_DATA",
            detail="no samples yet",
            remedy=_REMEDY["NO_DATA"],
            metrics=None,
        )

    mean_loss = sum(s.loss for s in samples) / n
    deg = [s for s in samples if s.degraded]
    ok = [s for s in samples if not s.degraded]
    deg_frac = len(deg) / n

    if mean_loss > RADIO_LOSS_PCT:
        verdict = "RADIO"
        detail = f"mean packet loss {mean_loss:.1f}% — the radio link is lossy"
    elif deg_frac < DEGRADED_FRAC:
        verdict = "HEALTHY"
        detail = f"only {deg_frac * 100:.0f}% of samples degraded; loss {mean_loss:.1f}%"
    else:
        r = _pearson([s.avg for s in samples], [s.load for s in samples])
        deg_load = sum(s.load for s in deg) / len(deg) if deg else 0.0
        ok_load = sum(s.load for s in ok) / len(ok) if ok else 0.0
        load_bound = r >= LOAD_PEARSON or (ok_load > 0 and deg_load >= ok_load * LOAD_RATIO)
        if load_bound:
            verdict = "LOAD"
            detail = (
                f"latency↔load r={r:.2f}; degraded windows avg load "
                f"{deg_load:.2f} vs healthy {ok_load:.2f}; loss {mean_loss:.1f}%"
            )
        else:
            verdict = "SCAN"
            detail = (
                f"{deg_frac * 100:.0f}% degraded but uncorrelated with load "
                f"(r={r:.2f}); loss {mean_loss:.1f}%"
            )

    avgs = sorted(s.avg for s in samples)
    metrics = Metrics(
        samples=n,
        degraded_pct=round(deg_frac * 100, 1),
        mean_loss_pct=round(mean_loss, 2),
        p50_avg_ms=round(avgs[n // 2], 1),
        worst_avg_ms=round(avgs[-1], 1),
    )
    return Diagnosis(
        verdict=verdict, detail=detail, remedy=_REMEDY[verdict], metrics=metrics
    )


# ─── sentinel assets (shipped by `sanctum link install`) ─────────────

SENTINEL_LABEL = "com.sanctum.wifi-stability"
SENTINEL_INTERVAL_S = 180

# The bash sampler. Read-only: it samples first-LAN-hop ping jitter (no Wi-Fi
# scan, so it never self-induces the jitter `system_profiler`/airport scans do)
# and logs one line per run, flagging degraded windows. Universal — the egress
# interface AND the ping target (the default gateway, i.e. the AP/router on a
# Wi-Fi node) are resolved at runtime, so there is NO hardcoded haus IP or iface.
SENTINEL_SCRIPT = r"""#!/bin/bash
# Sanctum Wi-Fi stability sentinel — samples this node -> first-LAN-hop ping
# jitter alongside the node's own load average. Read-only, ~3-min cadence via
# launchd. No Wi-Fi scan, so it never self-induces jitter.
LOGDIR="$HOME/.sanctum/logs"; LOG="$LOGDIR/wifi-stability.log"
umask 077            # the log carries the SSID (network name) — keep it 0600/dir 0700
mkdir -p "$LOGDIR"

# Default egress interface + gateway, resolved at runtime (no hardcoded values).
IFACE="$(route -n get default 2>/dev/null | awk -F': ' '/interface:/{print $2; exit}')"
[ -z "$IFACE" ] && IFACE="en0"
GW="$(route -n get default 2>/dev/null | awk -F': ' '/gateway:/{gsub(/ /,"",$2); print $2; exit}')"
[ -z "$GW" ] && GW="$(ipconfig getoption "$IFACE" router 2>/dev/null)"

# networksetup -getairportnetwork is broken on modern macOS (says "not
# associated" even when connected); ipconfig getsummary reads the live
# association without a scan.
SSID="$(ipconfig getsummary "$IFACE" 2>/dev/null | sed -nE 's/^[[:space:]]*SSID[[:space:]]*:[[:space:]]*(.+)$/\1/p' | head -1)"
[ -z "$SSID" ] && SSID="?"
LOAD="$(uptime | sed -E 's/.*load averages?: //')"

# No first-LAN-hop gateway found: do NOT fall back to pinging an external host —
# that would measure WAN, not the radio, and a node's WAN jitter/load would be
# misdiagnosed as a RADIO/LOAD problem. Record a tagged sample (excluded from the
# diagnosis) so the missing gateway is surfaced honestly, and stop here.
if [ -z "$GW" ]; then
  printf '%s ssid=%s rtt=NA loss=NA%% load=[%s] NO_GATEWAY\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S')" "${SSID:-?}" "$LOAD" >> "$LOG"
  exit 0
fi

OUT="$(ping -c 20 -i 0.2 -t 6 "$GW" 2>/dev/null)"
RTT="$(printf '%s\n' "$OUT" | grep -oE '= [0-9./]+ ms' | grep -oE '[0-9./]+')"   # min/avg/max/stddev
LOSS="$(printf '%s\n' "$OUT" | grep -oE '[0-9.]+% packet loss' | grep -oE '^[0-9.]+')"
AVG="$(printf '%s' "$RTT" | cut -d/ -f2)"; MAX="$(printf '%s' "$RTT" | cut -d/ -f3)"
FLAG="ok"
# degraded window: avg local ping > 20ms, OR max > 100ms, OR any loss
awk -v a="${AVG:-0}" -v m="${MAX:-0}" -v l="${LOSS:-0}" 'BEGIN{exit !(a+0>20 || m+0>100 || l+0>0)}' && FLAG="DEGRADED"
# On 100% loss `ping` prints no round-trip line, so RTT is empty -> rtt=NA; LOSS
# is still captured (100.0). The parser reads loss independently of rtt, so this
# total-loss window is NOT dropped — it drives the RADIO verdict.
printf '%s ssid=%s rtt=%s loss=%s%% load=[%s] %s\n' \
  "$(date '+%Y-%m-%dT%H:%M:%S')" "${SSID:-?}" "${RTT:-NA}" "${LOSS:-NA}" "$LOAD" "$FLAG" >> "$LOG"

# Cap the log so it cannot grow without bound (~10k samples; status only reads
# the most recent slice anyway). umask 077 keeps the rewritten file 0600.
tail -n 10000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
"""

# The LaunchAgent template. launchd does NOT expand ``~``, so the script path +
# error-log path MUST be absolute and per-user — :func:`render_plist` fills them
# from the path helpers below. The XML carries no other braces, so ``str.format``
# substitutes exactly the three named fields.
SENTINEL_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{script}</string>
  </array>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>{err_log}</string>
</dict>
</plist>
"""


def default_log_path() -> Path:
    """Where the sentinel writes its samples (and where ``status`` reads them)."""
    return Path.home() / ".sanctum" / "logs" / "wifi-stability.log"


def default_err_path() -> Path:
    """The plist's StandardErrorPath for the sentinel."""
    return Path.home() / ".sanctum" / "logs" / "wifi-stability.err"


def sentinel_script_path() -> Path:
    """Where ``install`` writes the bash sampler (0755)."""
    return Path.home() / ".sanctum" / "bin" / "wifi-stability-sentinel.sh"


def sentinel_plist_path() -> Path:
    """Where ``install`` writes the LaunchAgent plist."""
    return Path.home() / "Library" / "LaunchAgents" / f"{SENTINEL_LABEL}.plist"


def render_plist(*, script: Path | None = None, err_log: Path | None = None) -> str:
    """Render :data:`SENTINEL_PLIST` with absolute, per-user paths.

    Defaults resolve from the path helpers so the caller normally renders with no
    arguments; tests inject ``script`` / ``err_log`` to write into a temp dir.
    """
    script = script if script is not None else sentinel_script_path()
    err_log = err_log if err_log is not None else default_err_path()
    return SENTINEL_PLIST.format(
        label=SENTINEL_LABEL,
        script=script,
        interval=SENTINEL_INTERVAL_S,
        err_log=err_log,
    )


# ─── MAC stability (P2 — Optimize client: audit + enforce a stable MAC) ──
#
# Real incident: a closet Mac server's Wi-Fi was unstable for weeks because its
# "Private Wi-Fi Address" was set to Rotating (macOS re-defaulted it on a network
# re-join after a LAN renumber). A rotating Wi-Fi MAC makes the node periodically
# re-associate under a NEW MAC → the AP re-auths + re-DHCPs it every rotation →
# latency jitter + band-flapping. Pinning the node to its stable hardware MAC
# (Private Address Off) took the link from p50 35 ms / 90 %-degraded to p50 7.8 ms
# HEALTHY. This module AUDITS that on any node (:func:`analyze_mac`) and renders a
# configuration profile that ENFORCES a stable MAC so it can never silently revert
# (:func:`render_mac_stability_profile`). The system reads sit behind an injected
# runner (:func:`probe_wifi`) so the analysis is unit-testable without a radio.

CommandRunner = Callable[[list[str]], str]
"""A thin subprocess seam (argv → stdout) so :func:`probe_wifi` is testable."""

# A MAC: six 1-2 hex-digit octets, colon-separated. macOS zero-pads, but the
# pattern tolerates unpadded octets so a parse never silently drops a real MAC.
_MAC_PAT = r"((?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2})"
_ETHER_RE = re.compile(r"\bether\s+" + _MAC_PAT)
_GETMAC_RE = re.compile(r"Ethernet Address:\s*" + _MAC_PAT)
_SSID_RE = re.compile(r"^\s*SSID\s*:\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class MacAudit:
    """Verdict on a node's live Wi-Fi MAC: randomized (rotation risk) vs stable.

    Pure result of :func:`analyze_mac`. ``randomized`` is True when the live MAC
    differs from the burned-in hardware MAC (a private/rotating address); on a
    fixed-infra node that is the rotation-churn risk the optimizer exists to kill.
    """

    randomized: bool
    current: str
    hardware: str
    risk: str
    remedy: str


@dataclass(frozen=True)
class WifiProbe:
    """The thin impure read of the node's live Wi-Fi identity.

    ``current_mac`` is the live association MAC (``ifconfig <iface> ether`` — what
    the AP actually sees); ``hardware_mac`` is the burned-in address
    (``networksetup -getmacaddress``). They differ exactly when MAC randomization
    is on. ``ssid`` (live association, no scan) names the network the enforcement
    profile is scoped to; it is None when the node is not currently associated.
    """

    iface: str
    current_mac: str
    hardware_mac: str
    ssid: str | None


def is_locally_administered(mac: str) -> bool:
    """True if the MAC's locally-administered bit is set (a randomized address).

    macOS private/randomized Wi-Fi addresses are locally-administered: the
    second-least-significant bit of the first octet is 1. A burned-in hardware MAC
    is universally-administered (that bit is 0). A malformed/empty MAC returns
    False — it cannot be proven locally-administered.
    """
    first = mac.split(":", 1)[0]
    try:
        octet = int(first, 16)
    except ValueError:
        return False
    return bool(octet & 0b10)


_MAC_RISK_RANDOMIZED = (
    "private Wi-Fi MAC (differs from the hardware MAC). If it is Rotating — macOS's "
    "default after a network re-join — the node re-associates under a NEW MAC each "
    "rotation, so the AP re-auths + re-DHCPs it: latency jitter and band-flapping on "
    "a fixed-infra node whose only link is this radio. A Fixed private MAC is steady "
    "but still not the hardware MAC; a fixed-infra node should pin to hardware either "
    "way."
)
_MAC_REMEDY_RANDOMIZED = (
    "Pin the node to its hardware MAC: set Private Wi-Fi Address to Off for this "
    "network (System Settings ▸ Wi-Fi ▸ [network] ▸ Details…), or install the "
    "stability profile so macOS keeps it on the stable MAC and cannot silently "
    "flip it back to Rotating."
)
_MAC_RISK_STABLE = "none — the node is on its stable hardware MAC."
_MAC_REMEDY_STABLE = (
    "Keep Private Wi-Fi Address Off for this fixed-infra node so macOS cannot "
    "silently revert it to Rotating on a future network re-join."
)


def analyze_mac(current_mac: str, hardware_mac: str) -> MacAudit:
    """PURE: is the live Wi-Fi MAC randomized (differs from hardware) or stable?

    A randomized/private MAC differs from the burned-in hardware MAC (and carries
    the locally-administered bit). On a fixed-infra node — a closet server whose
    only link is Wi-Fi — a rotating MAC triggers periodic AP re-auth/re-DHCP
    (latency jitter + band-flapping), so it must be pinned to the hardware MAC.
    Equality is the definition; case is normalized because the two system reads
    can differ in case.
    """
    randomized = current_mac.lower() != hardware_mac.lower()
    return MacAudit(
        randomized=randomized,
        current=current_mac,
        hardware=hardware_mac,
        risk=_MAC_RISK_RANDOMIZED if randomized else _MAC_RISK_STABLE,
        remedy=_MAC_REMEDY_RANDOMIZED if randomized else _MAC_REMEDY_STABLE,
    )


# Fixed namespace for deterministic, content-derived profile UUIDs (NO Date.now /
# os.urandom): the same (ssid, hardware_mac) always renders identical bytes, so
# re-running optimize never churns the installed profile's identity.
_PROFILE_NS = uuid.UUID("5f4d4f3a-7e2b-5c1d-9a8b-1c2d3e4f5a6b")


def _slug(text: str) -> str:
    """Lowercase, reverse-DNS-safe slug for a PayloadIdentifier segment."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "sanctum"


def render_mac_stability_profile(
    ssid: str, hardware_mac: str, org: str = "Sanctum", encryption_type: str = "WPA3"
) -> str:
    """PURE: a ``.mobileconfig`` (plist XML) that DISABLES Wi-Fi MAC randomization.

    The payload is ``com.apple.wifi.managed`` for ``ssid`` with
    ``MACAddressRandomization`` set to ``False`` — Apple's key for "Private Wi-Fi
    Address: Off" — so macOS keeps the node on its stable hardware MAC and cannot
    silently flip it to Rotating. PayloadUUID / PayloadIdentifier are derived
    deterministically from ``ssid`` + ``hardware_mac`` via ``uuid5`` (no Date.now /
    random), so identical inputs render byte-identical output and re-applying never
    rotates the profile's identity. ``plistlib.dumps`` defaults to ``sort_keys``,
    so key order is stable too. Round-trips through :func:`plistlib.loads`.
    """
    seed = f"{ssid}\x00{hardware_mac.lower()}"
    inner_uuid = str(uuid.uuid5(_PROFILE_NS, f"wifi:{seed}")).upper()
    outer_uuid = str(uuid.uuid5(_PROFILE_NS, f"profile:{seed}")).upper()
    org_slug = _slug(org)

    wifi_payload: dict[str, object] = {
        "PayloadType": "com.apple.wifi.managed",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"{org_slug}.wifi-mac-stability.{inner_uuid}",
        "PayloadUUID": inner_uuid,
        "PayloadDisplayName": f"Wi-Fi ({ssid}) — stable MAC",
        "SSID_STR": ssid,
        "HIDDEN_NETWORK": False,
        # EncryptionType matches the saved network so the managed entry is not a
        # credential-less duplicate that disrupts the association (default WPA3;
        # pass encryption_type="WPA2"/"Any" for other networks).
        "EncryptionType": encryption_type,
        "AutoJoin": True,
        # The headline: false == "Private Wi-Fi Address: Off" for this network.
        "MACAddressRandomization": False,
    }
    profile: dict[str, object] = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"{org_slug}.wifi-mac-stability.profile.{outer_uuid}",
        "PayloadUUID": outer_uuid,
        "PayloadDisplayName": f"{org} Wi-Fi MAC Stability ({ssid})",
        "PayloadDescription": (
            "Disables Private/Rotating Wi-Fi Address for this network so a "
            "fixed-infra node stays on its stable hardware MAC. ADVANCED: this is a "
            "managed Wi-Fi payload — on approval macOS may re-prompt for the network "
            "password and briefly re-associate. Verify the connection survives "
            "before relying on it on a sole-link node; the zero-risk path is System "
            "Settings > Wi-Fi > Private Address > Off."
        ),
        "PayloadOrganization": org,
        "PayloadScope": "System",
        "PayloadContent": [wifi_payload],
    }
    return plistlib.dumps(profile, fmt=plistlib.FMT_XML).decode("utf-8")


def default_profile_path() -> Path:
    """Default ``--profile-out`` for the rendered stability ``.mobileconfig``."""
    return Path.home() / ".sanctum" / "wifi-mac-stability.mobileconfig"


def _real_run(argv: list[str], timeout: int = 8) -> str:
    """Default :data:`CommandRunner`: run ``argv``, return stdout, never raise."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return ""
    return proc.stdout


def _parse_wifi_iface(ports_text: str) -> str | None:
    """The BSD device (e.g. ``en0``) of the 'Wi-Fi' hardware port, or None.

    Parses ``networksetup -listallhardwareports`` — the Device line that follows a
    ``Hardware Port: Wi-Fi`` header.
    """
    port_name: str | None = None
    for line in ports_text.splitlines():
        s = line.strip()
        if s.startswith("Hardware Port:"):
            port_name = s.split(":", 1)[1].strip()
        elif s.startswith("Device:") and port_name is not None and "wi-fi" in port_name.lower():
            return s.split(":", 1)[1].strip() or None
    return None


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def probe_wifi(run: CommandRunner | None = None) -> WifiProbe:
    """Read the node's live Wi-Fi identity behind an injected runner.

    The impure boundary is kept thin — four system reads, no analysis: the Wi-Fi
    interface (``networksetup -listallhardwareports``), the live association MAC
    (``ifconfig <iface> ether``), the burned-in hardware MAC
    (``networksetup -getmacaddress <iface>``), and the live SSID
    (``ipconfig getsummary <iface>`` — no scan, so it never self-induces jitter).
    The verdict is the pure :func:`analyze_mac` over the result. ``run`` defaults
    to a real subprocess seam; tests inject a fake to drive it without a radio.
    """
    runner = run if run is not None else _real_run
    iface = _parse_wifi_iface(runner(["networksetup", "-listallhardwareports"]))
    if not iface:
        # Could NOT identify the Wi-Fi interface -> do NOT silently read en0 (on a
        # Mac mini that is *Ethernet*, whose MAC == its hardware MAC, yielding a
        # false "STABLE"). Return an UNVERIFIED probe (empty MACs) so the audit
        # honestly reports UNVERIFIED rather than a ✓ for the wrong radio.
        return WifiProbe(iface="", current_mac="", hardware_mac="", ssid=None)
    current = _first_match(_ETHER_RE, runner(["ifconfig", iface])) or ""
    hardware = _first_match(_GETMAC_RE, runner(["networksetup", "-getmacaddress", iface])) or ""
    ssid = _first_match(_SSID_RE, runner(["ipconfig", "getsummary", iface]))
    return WifiProbe(iface=iface, current_mac=current, hardware_mac=hardware, ssid=ssid)


# ─── Link Identity Guard (anti-quarantine: identity probe + diagnose) ──

_LINK_ACTIVE_RE = re.compile(r"LinkStatusActive\s*:\s*(TRUE|FALSE)", re.IGNORECASE)
_ROUTER_ARP_RE = re.compile(r"RouterARPVerified\s*:\s*(TRUE|FALSE)", re.IGNORECASE)
_SECURITY_RE = re.compile(r"^\s*Security\s*:\s*(.+?)\s*$", re.MULTILINE)
_GW_RE = re.compile(r"gateway:\s*([0-9a-fA-F:.]+)")
# The packet-loss percentage from a ping summary. Anchored on a word boundary so
# "0.0%" is NOT read out of "100.0%" (a naive `"0.0% packet loss" in out` substring
# check reports total loss as reachable — the exact false-STABLE this guards against).
_PING_LOSS_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)%\s*packet loss")


@dataclass(frozen=True)
class IdentityProbe:
    """Live network *identity* facts (distinct from the sentinel link-health log).

    Read from ``ipconfig getsummary`` (+ one bounded gateway ping) — router-agnostic.
    ``router_arp_verified``/``gateway_reachable`` are None when unknown/unattempted;
    empty ``iface`` (or MACs) means UNVERIFIED — never a silent false-STABLE.
    """

    iface: str
    ssid: str | None
    current_mac: str
    hardware_mac: str
    security: str | None
    associated: bool
    router_arp_verified: bool | None
    gateway_reachable: bool | None


def _enc_from_security(security: str | None) -> str:
    """Map an ``ipconfig getsummary`` Security value to a profile EncryptionType.

    A mismatch would make macOS create a non-joining duplicate network, so this is
    a hard requirement, not a guess; WPA2 is the safe default (covers WPA2/WPA3
    personal transition).
    """
    if security and "WPA3" in security.upper():
        return "WPA3"
    return "WPA2"


def _tri(pattern: re.Pattern[str], text: str) -> bool | None:
    """TRUE/FALSE → bool; field absent → None (unknown, fail-open to None)."""
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1).upper() == "TRUE"


def probe_identity(run: CommandRunner | None = None, *, ping_gateway: bool = True) -> IdentityProbe:
    """Read the node's live Wi-Fi *identity + reachability* behind an injected runner.

    Thin impure boundary: interface + MACs (reused from :func:`probe_wifi`'s reads),
    plus ``ipconfig getsummary`` association/RouterARPVerified/Security, plus a
    bounded default-gateway ping. Fail-closed: no Wi-Fi iface → UNVERIFIED probe.
    """
    runner = run if run is not None else _real_run
    iface = _parse_wifi_iface(runner(["networksetup", "-listallhardwareports"]))
    if not iface:
        return IdentityProbe(
            iface="", ssid=None, current_mac="", hardware_mac="", security=None,
            associated=False, router_arp_verified=None, gateway_reachable=None,
        )
    current = _first_match(_ETHER_RE, runner(["ifconfig", iface])) or ""
    hardware = _first_match(_GETMAC_RE, runner(["networksetup", "-getmacaddress", iface])) or ""
    summary = runner(["ipconfig", "getsummary", iface])
    ssid = _first_match(_SSID_RE, summary)
    security = _first_match(_SECURITY_RE, summary)
    associated = _tri(_LINK_ACTIVE_RE, summary) is True
    arp = _tri(_ROUTER_ARP_RE, summary)

    gw_reachable: bool | None = None
    if ping_gateway and associated:
        gw = _first_match(_GW_RE, runner(["route", "-n", "get", "default"]))
        if gw:
            out = runner(["ping", "-c", "3", "-t", "2", gw])
            loss = _first_match(_PING_LOSS_RE, out)
            # Reachable when the summary reports <100% loss; a parse-failure leaves it
            # None (unknown) so we never claim reachable from an unread ping.
            gw_reachable = float(loss) < 100.0 if loss is not None else None
    return IdentityProbe(
        iface=iface, ssid=ssid, current_mac=current, hardware_mac=hardware,
        security=security, associated=associated,
        router_arp_verified=arp, gateway_reachable=gw_reachable,
    )


_IDENTITY_REMEDY: dict[str, str] = {
    "IDENTITY_QUARANTINED": (
        "This node is associated but its gateway is unreachable while it presents a "
        "rotating/private MAC — the router does not recognize it (a DHCP-reservation "
        "miss or device quarantine). Pin it to its hardware MAC: sanctum link optimize "
        "--apply (or Private Wi-Fi Address ▸ Off), then re-verify."
    ),
    "IDENTITY_ROTATING": (
        "MAC is randomized (private address) — it works now but will islande the node "
        "on the next router-trust reset (reboot/renumber). Pin to the hardware MAC: "
        "sanctum link optimize --apply."
    ),
    "IDENTITY_STABLE": "Identity is correct — the node is on its stable hardware MAC.",
    "IDENTITY_UNVERIFIED": "Could not read the Wi-Fi identity — is this node associated? Re-run when connected.",
}


@dataclass(frozen=True)
class IdentityDiagnosis:
    """The IDENTITY verdict (who the node is on the network) + remedy + probe."""

    verdict: str
    detail: str
    remedy: str
    probe: IdentityProbe


def diagnose_identity(probe: IdentityProbe) -> IdentityDiagnosis:
    """PURE: classify the node's Wi-Fi *identity* (orthogonal to link health).

    UNVERIFIED (fail-closed) when we cannot read it; QUARANTINED for the exact
    incident signature (associated + LAN-dead + rotating MAC); ROTATING for the
    at-risk private-MAC case; STABLE when on the hardware MAC.
    """
    if not probe.iface or not probe.current_mac or not probe.hardware_mac or not probe.associated:
        v = "IDENTITY_UNVERIFIED"
        return IdentityDiagnosis(v, "identity could not be read", _IDENTITY_REMEDY[v], probe)
    randomized = (
        is_locally_administered(probe.current_mac)
        or probe.current_mac.lower() != probe.hardware_mac.lower()
    )
    lan_dead = probe.router_arp_verified is False or probe.gateway_reachable is False
    if randomized and lan_dead:
        v, detail = "IDENTITY_QUARANTINED", (
            f"associated on {probe.iface} but gateway unreachable "
            f"(RouterARPVerified={probe.router_arp_verified}) while presenting "
            f"rotating MAC {probe.current_mac} ≠ hardware {probe.hardware_mac}"
        )
    elif randomized:
        v, detail = "IDENTITY_ROTATING", (
            f"rotating MAC {probe.current_mac} ≠ hardware {probe.hardware_mac}; reachable for now"
        )
    else:
        note = "" if not lan_dead else " (gateway unreachable — see `sanctum link status` for a link-health read)"
        v, detail = "IDENTITY_STABLE", f"on hardware MAC {probe.hardware_mac}{note}"
    return IdentityDiagnosis(v, detail, _IDENTITY_REMEDY[v], probe)


# ─── node classification (SERVER auto-enroll vs ROAMER opt-in) ─────────

SERVER_UPTIME_DAYS = 3.0
SSID_ROAMER_THRESHOLD = 5
SSID_SERVER_MAX = 3


@dataclass(frozen=True)
class NodeSignals:
    uptime_days: float
    ip_config_method: str  # "Manual" | "DHCP" | ""
    ip_is_reserved_or_static: bool
    distinct_ssids_seen: int
    is_portable: bool


@dataclass(frozen=True)
class NodeClass:
    klass: str  # "SERVER" | "ROAMER" | "UNKNOWN"
    reason: str


def classify_node(signals: NodeSignals) -> NodeClass:
    """PURE: is this a fixed-infra SERVER (auto-enroll) or a ROAMER (opt-in only)?

    Conservative and privacy-first: portability or many-SSIDs → ROAMER; a
    non-portable, long-lived / static-or-reserved, single-SSID node → SERVER;
    anything ambiguous → UNKNOWN (treated as ROAMER downstream, never auto-enrolled).
    """
    if signals.is_portable or signals.distinct_ssids_seen > SSID_ROAMER_THRESHOLD:
        return NodeClass("ROAMER", "portable or roams across many networks")
    fixed = signals.uptime_days >= SERVER_UPTIME_DAYS or signals.ip_is_reserved_or_static
    if not signals.is_portable and fixed and signals.distinct_ssids_seen <= SSID_SERVER_MAX:
        return NodeClass("SERVER", "always-on / static-or-reserved IP / single network")
    return NodeClass("UNKNOWN", "insufficient signal — treated as roamer (privacy-first)")


# ─── Task 4: optional Firewalla quarantine enrichment ────────────────

QuarantineTransport = Callable[[str], str]
"""mac → raw tags response (e.g. redis ``hget policy:mac:<MAC> tags``); "" when absent."""


@dataclass(frozen=True)
class QuarantineFinding:
    quarantined: bool
    tag: str | None
    detail: str


def firewalla_quarantine_check(
    mac: str, transport: QuarantineTransport | None = None
) -> QuarantineFinding | None:
    """OPTIONAL enrichment: is ``mac`` in a Firewalla quarantine tag?

    Returns None when no Firewalla transport is wired or it does not answer — the
    router-agnostic path never depends on this. When a transport answers with a
    tags array, a non-empty tag list means quarantined (tag "18" == the DAP
    Quarantine group observed in the incident).
    """
    if transport is None:
        return None
    raw = transport(mac).strip()
    if not raw:
        return None
    tags = re.findall(r'"(\d+)"', raw)
    if not tags:
        return QuarantineFinding(False, None, "device present, no quarantine tag")
    return QuarantineFinding(True, tags[0], f"in Firewalla tag {tags[0]} (quarantine)")
