#!/usr/bin/env python3
"""endocrine-gland-sentinel.py — watch the seventh organ.

The endocrine system is a NEW organ; the Living Force (sanctum-watchdog) must be
able to see it. This sentinel is OBSERVE-ONLY: it reads the gland's published
panel + freshness and pages Force Flow ONLY on a PATHOLOGICAL state. It never
restarts, kills, or doses anything.

It is DAMPED by construction, reusing the sibling's already-shipped
``alert-confirm.sh`` (probe-twice + cooldown) so it cannot become an alert-storm
source — the truthful-alerts lesson made structural (0 false criticals). The
endocrine layer is ITSELF feedback-damped (the regulator math forbids an
out-of-range level), so an out-of-range pathology here means the gland is
mis-running — a real, page-worthy organ fault, never the math running away.

Pages (any → CRITICAL, confirm-twice + 30-min cooldown):
  GLAND_DOWN   — the gland launchd job is unloaded, OR loaded but its last tick
                 exited non-zero with no panel (crashing), OR the panel file is
                 stale > STALE_SEC / unparseable (the tick loop has stopped — the
                 organ is dead)
  HORMONE_STORM— a published level is out of [0,1] (regulator invariant
                 violated → mis-running gland; the math cannot produce this) OR
                 cortisol pinned high (>= STORM_CORTISOL) for the whole confirm
                 window. STORM_CORTISOL (0.97) sits strictly ABOVE the gland's
                 max legitimate cortisol target (_CORTISOL_TARGET_MAX = 0.95),
                 so the cortisol-pinned branch fires only on a genuinely STUCK
                 stress axis — NOT on a faithful read of a real crisis. (Keep
                 STORM_CORTISOL > the gland's max reachable target if either moves.)

Observed, NEVER pages:
  GLAND_STARTING— no panel yet but the gland job is loaded and its last tick was
                 clean (the organ is being born — a few seconds at boot). An organ
                 is not "dead" the instant it loads; this is the boot-race fix.

Modes:
  (default)    diagnose + page Force Flow on pathological (confirm + cooldown)
  --check      dry-run: print the JSON verdict, never page
  --self-test  run the detector against synthetic fixtures + report whether the
               matching launchd label is LOADED, never touch state

Run by com.sanctum.endocrine-gland-sentinel.plist every 300s. LOADED on manoir
as of 2026-06-15 (the endocrine organ is live); on a host where it is not yet
bootstrapped, `--self-test` says so plainly.

This file is the deploy copy; the canonical source is in the sanctum-cli repo
under deploy/endocrine/. It depends ONLY on the stdlib + alert-confirm.sh, so it
runs from ~/.sanctum without the sanctum_cli package on PYTHONPATH.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HOME = os.environ.get("HOME", "/Users/bert")
PANEL_FILE = os.environ.get(
    "ENDOCRINE_PANEL_FILE", os.path.join(HOME, ".sanctum/state/endocrine/panel.json")
)
STATE_FILE = os.environ.get(
    "ENDOCRINE_SENTINEL_STATE",
    os.path.join(HOME, ".sanctum/state/endocrine-gland-sentinel.json"),
)
FORCE_FLOW_URL = os.environ.get("FORCE_FLOW_URL", "http://127.0.0.1:4077")
ALERT_CONFIRM = os.environ.get(
    "ALERT_CONFIRM_LIB", os.path.join(HOME, ".sanctum/lib/alert-confirm.sh")
)
# The gland ticks every ~60s; a panel older than this means the loop stopped.
STALE_SEC = int(os.environ.get("ENDOCRINE_STALE_SEC", "600"))  # 10 min (10 missed ticks)
STORM_CORTISOL = float(os.environ.get("ENDOCRINE_STORM_CORTISOL", "0.97"))
ALERT_COOLDOWN = int(os.environ.get("ENDOCRINE_ALERT_COOLDOWN", "1800"))  # 30 min
HORMONES = (
    "dopamine", "cortisol", "noradrenaline", "oxytocin", "melatonin", "serotonin",
)
NOW = int(time.time())


def read_panel() -> tuple[dict | None, int | None]:
    """Return (panel_dict, age_sec). (None, None) if unreadable/absent."""
    try:
        age = NOW - int(os.path.getmtime(PANEL_FILE))
    except OSError:
        return None, None
    try:
        with open(PANEL_FILE) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None, age
    panel = doc.get("panel", doc) if isinstance(doc, dict) else None
    if not isinstance(panel, dict):
        return None, age
    return panel, age


def gland_status() -> tuple[bool, int | None]:
    """(job_loaded, last_exit_code) for the gland launchd job, via `launchctl print`.

    Pure-ish side helper kept OUT of `diagnose` so the verdict stays unit-pure.
    Returns (False, None) if the job is not loaded or launchctl is unreadable;
    (True, None) if loaded but it has not exited yet (just bootstrapped)."""
    label = os.environ.get("ENDOCRINE_GLAND_LABEL", "com.sanctum.endocrine-gland")
    try:
        out = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False, None
    if out.returncode != 0:
        return False, None  # not loaded
    last_exit: int | None = None
    for line in out.stdout.splitlines():
        s = line.strip()
        if s.startswith("last exit code ="):
            try:
                last_exit = int(s.split("=", 1)[1].strip())
            except ValueError:
                last_exit = None
            break
    return True, last_exit


def diagnose(
    panel: dict | None,
    age: int | None,
    gland_loaded: bool = False,
    gland_last_exit: int | None = None,
) -> tuple[str, str]:
    """Pure verdict. Returns (state, detail). HEALTHY when the organ is fine.

    The absent-panel case is disambiguated by the gland launchd job's state so a
    freshly-bootstrapped organ that simply hasn't published its first beat yet is
    GLAND_STARTING (NOT pageable) — never a critical "the organ is dead" the
    instant it boots. It escalates to GLAND_DOWN only when the job is unloaded or
    its last tick exited non-zero (crashing)."""
    if panel is None:
        if age is None:
            if not gland_loaded:
                return "GLAND_DOWN", (
                    f"no panel at {PANEL_FILE} and the gland launchd job is NOT "
                    f"loaded — the organ is down"
                )
            if gland_last_exit not in (0, None):
                return "GLAND_DOWN", (
                    f"no panel at {PANEL_FILE}; gland job loaded but its last tick "
                    f"exited {gland_last_exit} — the organ is crashing"
                )
            return "GLAND_STARTING", (
                "no panel yet but the gland job is loaded and last tick was clean "
                "— the organ is starting (first beat pending)"
            )
        return "GLAND_DOWN", (
            f"panel file present but unparseable (age {age}s) — gland mis-writing"
        )
    if age is not None and age > STALE_SEC:
        return "GLAND_DOWN", (
            f"panel stale {age}s (> {STALE_SEC}s) — the gland tick loop has "
            f"stopped; disposition is frozen"
        )
    # invariant: every level must be a real number in [0,1]
    for h in HORMONES:
        v = panel.get(h)
        if not isinstance(v, (int, float)) or v != v or v < 0.0 or v > 1.0:
            return "HORMONE_STORM", (
                f"{h}={v!r} out of [0,1] — regulator invariant violated; the "
                f"gland is mis-running (the math cannot produce this)"
            )
    cort = panel.get("cortisol", 0.0)
    if isinstance(cort, (int, float)) and cort >= STORM_CORTISOL:
        return "HORMONE_STORM", (
            f"cortisol pinned high ({cort:.2f} >= {STORM_CORTISOL}) — stuck "
            f"stress axis; the council is jammed convergent"
        )
    return "HEALTHY", (
        f"gland fresh ({age}s); levels in range; "
        f"cortisol={cort:.2f} dopamine={panel.get('dopamine')}"
    )


def confirm_down_via_lib(state: str) -> bool:
    """Re-probe the verdict through alert-confirm.sh's confirm_down (probe-twice).

    A second read that comes back HEALTHY means the first was transient → do NOT
    page. We shell into the sibling lib so the damping is the EXACT same code the
    rest of the haus uses (no re-implementation). If the lib is missing we fall
    back to confirming in-process with one extra read."""
    probe = (
        f'{sys.executable} {os.path.abspath(__file__)} --check '
        f'| grep -q \'"state": "{state}"\''
    )
    if os.path.exists(ALERT_CONFIRM):
        cmd = f'source "{ALERT_CONFIRM}"; confirm_down \'{probe}\''
        try:
            rc = subprocess.run(
                ["bash", "-c", cmd], capture_output=True, text=True, timeout=30
            ).returncode
        except Exception:
            return False
        return rc == 0  # 0 == CONFIRMED (both reads agreed it's bad)
    # fallback: one extra in-process read
    time.sleep(3)
    panel, age = read_panel()
    again, _ = diagnose(panel, age)
    return again == state


def cooldown_ok_via_lib(key: str) -> bool:
    """Anti-storm cooldown through alert-confirm.sh's alert_cooldown_ok."""
    if os.path.exists(ALERT_CONFIRM):
        cmd = f'source "{ALERT_CONFIRM}"; alert_cooldown_ok "{key}" {ALERT_COOLDOWN}'
        try:
            rc = subprocess.run(
                ["bash", "-c", cmd], capture_output=True, text=True, timeout=10
            ).returncode
        except Exception:
            return False
        return rc == 0
    # fallback cooldown via local state file
    prev = load_state()
    return (NOW - int(prev.get("last_alert", 0) or 0)) > ALERT_COOLDOWN


def force_flow_notify(severity: str, title: str, message: str) -> None:
    payload = json.dumps({
        "source": "endocrine-gland-sentinel",
        "severity": severity, "title": title, "message": message,
    })
    try:
        subprocess.run(
            ["curl", "-fsS", "-X", "POST", f"{FORCE_FLOW_URL}/notify",
             "-H", "Content-Type: application/json", "--max-time", "10", "-d", payload],
            capture_output=True, text=True, timeout=12,
        )
    except Exception:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(d: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(d, f, indent=2)


# (panel, age, gland_loaded, gland_last_exit, want)
SYNTHETIC = {
    "healthy":          ({"dopamine": 0.3, "cortisol": 0.2, "noradrenaline": 0.1,
                          "oxytocin": 0.5, "melatonin": 0.2, "serotonin": 0.6}, 30, True, 0, "HEALTHY"),
    "stale":            ({"dopamine": 0.3, "cortisol": 0.2, "noradrenaline": 0.1,
                          "oxytocin": 0.5, "melatonin": 0.2, "serotonin": 0.6}, 9000, True, 0, "GLAND_DOWN"),
    # absent panel is disambiguated by the gland job state (the boot-race fix):
    "absent_starting":  (None, None, True, 0, "GLAND_STARTING"),     # loaded, clean → being born, NO page
    "absent_neverran":  (None, None, True, None, "GLAND_STARTING"),  # loaded, not yet exited → being born
    "absent_crashing":  (None, None, True, 78, "GLAND_DOWN"),        # loaded but last tick crashed → DOWN
    "absent_unloaded":  (None, None, False, None, "GLAND_DOWN"),     # job not loaded → truly DOWN
    "oor":              ({"dopamine": 1.7, "cortisol": 0.2, "noradrenaline": 0.1,
                          "oxytocin": 0.5, "melatonin": 0.2, "serotonin": 0.6}, 30, True, 0, "HORMONE_STORM"),
    "stuckhigh":        ({"dopamine": 0.1, "cortisol": 0.99, "noradrenaline": 0.8,
                          "oxytocin": 0.5, "melatonin": 0.2, "serotonin": 0.2}, 30, True, 0, "HORMONE_STORM"),
}


def _sentinel_label_loaded() -> bool | None:
    """Is THIS sentinel's launchd job loaded? (True/False, or None if launchctl
    is unreadable). Honest about the deploy state so --self-test can report
    LOADED vs not-yet-bootstrapped rather than implying it's always running."""
    label = os.environ.get(
        "ENDOCRINE_SENTINEL_LABEL", "com.sanctum.endocrine-gland-sentinel"
    )
    try:
        out = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    return out.returncode == 0


def self_test() -> int:
    ok = True
    for name, (panel, age, loaded, last_exit, want) in SYNTHETIC.items():
        state, _ = diagnose(panel, age, loaded, last_exit)
        passed = state == want
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name:10s} -> {state} (want {want})")
    # Deploy honesty: report whether the sentinel's own launchd label is loaded.
    # This is observational (never affects the PASS/FAIL exit) — it tells the
    # operator if the sentinel is actually running on this host.
    sent_loaded = _sentinel_label_loaded()
    if sent_loaded is None:
        loaded_txt = "launchd unreadable (cannot tell)"
    elif sent_loaded:
        loaded_txt = "LOADED"
    else:
        loaded_txt = "NOT loaded (not bootstrapped on this host)"
    print(f"  [info] sentinel launchd label -> {loaded_txt}")
    print("self-test:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main() -> None:
    args = sys.argv[1:]
    if "--self-test" in args:
        sys.exit(self_test())

    panel, age = read_panel()
    loaded, last_exit = gland_status()
    state, detail = diagnose(panel, age, loaded, last_exit)
    verdict = {"ts": NOW, "state": state, "detail": detail, "age": age}

    if "--check" in args:
        print(json.dumps(verdict))
        return

    # Only genuinely-pathological states page. HEALTHY and GLAND_STARTING (the
    # organ is alive and being born) never page — the boot-race false-critical fix.
    pageable = ("GLAND_DOWN", "HORMONE_STORM")
    if state in pageable:
        # DAMPED: confirm via the sibling probe-twice lib, then cooldown-gate.
        if confirm_down_via_lib(state) and cooldown_ok_via_lib(f"endocrine-{state.lower()}"):
            force_flow_notify(
                "critical",
                f"Endocrine {state} — the seventh organ is unhealthy",
                f"{detail}  [observe-only; investigate: cat {PANEL_FILE}; "
                f"check com.sanctum.endocrine-gland launchd job]",
            )
            write_state({"last_alert": NOW, "last_state": state, "detail": detail})
    print(json.dumps(verdict))


if __name__ == "__main__":
    main()
