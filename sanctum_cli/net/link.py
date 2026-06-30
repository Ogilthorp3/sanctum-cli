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
import re
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
