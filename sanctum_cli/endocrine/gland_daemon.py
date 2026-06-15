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
  • alert rate       ← Force-Flow ``/history`` (count of critical/error/p0 rows
                       in the last hour) → noradrenaline + sustained cortisol.
                       Honest blind (None) if the endpoint isn't reachable.
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
    """Count high-severity Force-Flow notifications in the last hour.

    → noradrenaline (acute) + sustained cortisol. Force-Flow exposes
    GET /history (force_flow.py:608) — a bare JSON ARRAY of ``notifications``
    rows, each a dict with ``severity`` (TEXT) and ``timestamp`` (TEXT, ISO-8601,
    naive local-time as written by force_flow). We request the hot severities and
    count those inside a 1h window. None on ANY read failure — honest blindness
    (no fabricated zero); a None on this axis simply means noradrenaline and the
    alert-driven cortisol top-up stay quiescent this tick.

    Force-Flow has two coexisting severity grammars (force_flow.py:301-313):
    {critical,error,warn,info} and {p0,p1,p2}, with p0=critical/p1=error. We
    cover the hot ones (critical/error/p0) explicitly — the stored string is what
    /history returns, so we filter on the literal values, not the aliases."""
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    hot = ("critical", "error", "p0")
    base = bloodstream.force_flow_base()
    total = 0
    for sev in hot:
        try:
            url = f"{base}/history?severity={sev}&limit=200"
            with urllib.request.urlopen(url, timeout=timeout) as r:
                rows = json.loads(r.read())
        except Exception:
            return None  # any axis-blind read → honest None, never a partial count
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts = row.get("timestamp")
            if not isinstance(ts, str):
                continue
            try:
                dt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            # force_flow writes naive local timestamps; treat tz-naive as UTC so
            # the window math is comparable (a small skew only widens the window).
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.UTC)
            if 0 <= (now - dt).total_seconds() <= 3600:
                total += 1
    return total


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
