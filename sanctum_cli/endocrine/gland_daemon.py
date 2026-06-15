"""The GLAND DAEMON — the live organ.

Wraps the pure regulator (:mod:`gland`) in a loop that reads REAL local,
non-sensitive signals, steps the homeostat ONCE per tick (slow modulation —
the panel LAGS the signals on purpose), and broadcasts the result on the
bloodstream (chitti samskara + the query file).

Signals (all local telemetry, no PII, no secrets):
  • memory pressure  ← chitti ``/fluid`` ``kosha.annamaya.memory_available_gb``
                       (the SAME fluid the ram-sentinel reads) → cortisol.
                       This is castellan's pressure made available without a
                       privileged read; headroom_mb = available_gb·1024.
  • alert rate       ← Force-Flow ``/recent`` (count of critical/error in the
                       last hour) → noradrenaline + sustained cortisol. Honest
                       blind (None) if the endpoint isn't reachable.
  • time-of-day      ← local clock hour → melatonin (circadian).
  • creative mode    ← the operator lever file → dopamine↑ / cortisol↓.

Tick cadence is SLOW (default 60 s): the endocrine system is the slow modulator
by design. One step per tick means a real signal change takes several minutes to
fully express in the panel — exactly the "state, not reflex" property.

Persistence: the live level vector is checkpointed to the panel file each tick,
so a restart resumes from the last disposition rather than snapping to neutral
(the homeostat picks up where it left off). On a cold start with no checkpoint
it begins at :meth:`Panel.neutral` — off-by-default.

Modes:
  (default)   run forever, one tick per ``--interval`` seconds
  --once      do exactly one tick (read → step → broadcast) and exit. This is
              what the staged plist runs on a StartInterval, and what the
              real-loop test drives — no long-lived process required.
  --dry-run   read signals + compute the next panel, print it, broadcast
              NOTHING (no chitti write, no file write).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

from . import bloodstream
from .gland import Panel, Signals, step_panel


def read_memory_headroom_mb(*, timeout: float = 4.0) -> int | None:
    """Memory headroom from chitti /fluid → cortisol. None if blind.

    Reads ``kosha.annamaya.memory_available_gb`` (the live fluid shape verified
    2026-06-15) and converts to MB. A None return is HONEST BLINDNESS: the gland
    holds cortisol toward setpoint rather than fabricating "all calm" (the
    no-fabrication doctrine — a blind read is not a healthy read)."""
    try:
        with urllib.request.urlopen(f"{bloodstream.chitti_base()}/fluid", timeout=timeout) as r:
            doc = json.loads(r.read())
    except Exception:
        return None
    try:
        avail_gb = doc["kosha"]["annamaya"]["memory_available_gb"]
        return int(float(avail_gb) * 1024)
    except (KeyError, TypeError, ValueError):
        return None


def read_alert_rate_1h(*, timeout: float = 4.0) -> int | None:
    """Count of recent critical/error Force-Flow alerts in the last hour.

    → noradrenaline (acute) + sustained cortisol. Force-Flow exposes recent
    notifications; we count the high-severity ones in a 1h window. None on any
    read failure (honest blindness — never a fabricated zero)."""
    try:
        with urllib.request.urlopen(
            f"{bloodstream.force_flow_base()}/recent", timeout=timeout
        ) as r:
            doc = json.loads(r.read())
    except Exception:
        return None
    items = doc.get("items") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        return None
    now = time.time()
    hot = {"critical", "error", "p0"}
    count = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        sev = str(it.get("severity", "")).lower()
        if sev not in hot:
            continue
        ts = it.get("ts") or it.get("epoch")
        # accept epoch seconds or an ISO string; out-of-window items are skipped
        if isinstance(ts, (int, float)):
            if now - ts <= 3600:
                count += 1
        else:
            count += 1  # no timestamp → count it (conservative: never undercount stress)
    return count


def read_signals(*, creative: bool | None = None) -> Signals:
    """Assemble the REAL signal vector for this tick."""
    if creative is None:
        creative = bloodstream.read_creative_mode()
    return Signals(
        headroom_mb=read_memory_headroom_mb(),
        alert_rate_1h=read_alert_rate_1h(),
        hour=time.localtime().tm_hour,
        creative_mode=creative,
    )


def load_checkpoint() -> Panel:
    """Resume from the last published panel, or start neutral (off-by-default)."""
    p = bloodstream.read_panel()
    return p if p is not None else Panel.neutral()


def tick(*, dry_run: bool = False) -> Panel:
    """One homeostatic tick: read real signals → step → broadcast.

    Returns the new panel. With ``dry_run`` it computes but broadcasts nothing.
    """
    current = load_checkpoint()
    signals = read_signals()
    nxt = step_panel(current, signals)
    if not dry_run:
        bloodstream.publish_panel_file(nxt)
        bloodstream.broadcast_to_chitti(nxt)
    return nxt


def _print_panel(panel: Panel, signals: Signals | None = None) -> None:
    out: dict[str, object] = {"panel": panel.to_wire()}
    if signals is not None:
        out["signals"] = {
            "headroom_mb": signals.headroom_mb,
            "alert_rate_1h": signals.alert_rate_1h,
            "hour": signals.hour,
            "creative_mode": signals.creative_mode,
        }
    print(json.dumps(out, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sanctum endocrine gland daemon.")
    ap.add_argument("--once", action="store_true", help="one tick then exit")
    ap.add_argument("--dry-run", action="store_true", help="compute, broadcast nothing")
    ap.add_argument("--interval", type=int, default=60, help="tick cadence in seconds (default 60)")
    args = ap.parse_args(argv)

    if args.once or args.dry_run:
        signals = read_signals()
        current = load_checkpoint()
        nxt = step_panel(current, signals)
        if not args.dry_run:
            bloodstream.publish_panel_file(nxt)
            bloodstream.broadcast_to_chitti(nxt)
        _print_panel(nxt, signals)
        return 0

    # long-running loop (the daemon proper). Each tick is independently
    # bounded; a single failed read cannot wedge it (read_* returns None).
    while True:
        try:
            tick()
        except Exception as e:
            print(f"endocrine tick error (non-fatal): {e}", file=sys.stderr)
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
