"""The GLAND — hypothalamus-pituitary of the council.

Reads REAL local signals (castellan memory pressure, Force-Flow alert rate,
time-of-day, an explicit creative-mode input), runs a homeostatic regulator,
and produces the hormone Panel that gets broadcast on the bloodstream
(chitti samskara + a queryable /panel endpoint).

═══════════════════════════════════════════════════════════════════════════
THE MATH — a first-order leaky integrator (negative-feedback regulator)
═══════════════════════════════════════════════════════════════════════════

Each hormone h ∈ [0,1] obeys, every tick:

    drive[t]  = clamp(raw_drive[t], -DRIVE_MAX, +DRIVE_MAX)      # bounded input
    h[t+1]    = clamp01( h[t] + leak·(setpoint − h[t]) + drive[t] )

This is the discrete first-order system  h[t+1] = (1−leak)·h[t] + leak·setpoint + drive.

WHY IT CANNOT RUN AWAY (the structural anti-"hormone-storm" property):

  1. The homogeneous part (drive=0) is  h ← (1−leak)·h + leak·setpoint.
     With 0 < leak < 1 the multiplier (1−leak) ∈ (0,1) is a CONTRACTION:
     |h[t]−setpoint| = (1−leak)^t · |h[0]−setpoint| → 0 geometrically.
     There is exactly one fixed point (h* = setpoint) and it is attracting.
     ⇒ remove all drive and every hormone decays back to its setpoint. No
       integrator wind-up, no oscillation, no divergence.

  2. The forced part is BIBO-stable: the per-step clamp01 keeps h in [0,1] for
     ANY drive whatsoever (even ±∞). The bound on the OUTPUT is the storm-proof
     — there is no input that can push a level out of range, ever. This is the
     truthful-alerts lesson made structural: the regulator cannot be the source
     of a runaway, so pathology alerts (which the sentinel raises) can only come
     from a STUCK input, never from the math.

  3. Cross-coupling is ANTAGONISTIC ONLY (cortisol ⊣ dopamine). It enters as a
     NEGATIVE term on dopamine's drive proportional to cortisol's level. A
     negative coupling can only damp; it can never form the positive loop that
     would be required for amplification. (Biologically exact: cortisol/stress
     suppresses divergent thinking; dopamine fuels it.)

`settle(signals, iterations)` runs the map to its fixed point for the current
signals — useful for a one-shot "what's the panel right now" read and for the
tests. The live daemon instead `step()`s once per tick (slow modulation), so
the panel LAGS the signals (a feature: slow, not reflexive).

The EWMA/decay design is a deliberate 1:1 port of chitti's `mood.rs`
(MoodTracker / compute_pressure_score) so the eventual Rust promotion is
mechanical and the dynamics are derived from a proven, different-author source.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, ClassVar

# ── hormone roster (named scalar levels 0.0..1.0 — the council's "panel") ──
# Order matters only for stable iteration/serialization.
HORMONES: tuple[str, ...] = (
    "dopamine",  # drive/novelty/exploration → RAISES creativity
    "cortisol",  # stress → SUPPRESSES creativity (memory pressure + alert rate)
    "noradrenaline",  # acute arousal → incident sharpening (spikes, decays)
    "oxytocin",  # trust/cohesion → consensus vs independent (LOW = red-team)
    "melatonin",  # circadian → quiet-hours terseness / defer
    "serotonin",  # baseline mood/confidence/stability
)

# Per-step drive clamp. Even a pathological signal cannot move a hormone more
# than this per tick — bounds the slew rate (a second damping layer on top of
# the output clamp). 0.5 means worst case ~2 ticks to cross the whole range,
# but the leak pulls back every tick so steady-state stays bounded well inside.
DRIVE_MAX = 0.5

# castellan pressure tiers (read from main_loop.rs:190 — NOT reinvented).
# headroom_mb thresholds: <0 Catastrophic, <2048 Critical, <6144 Warn, else Normal.
_CASTELLAN_CATASTROPHIC_MB = 0
_CASTELLAN_CRITICAL_MB = 2048
_CASTELLAN_WARN_MB = 6144

# Force-Flow quiet-hours grammar (force_flow.py:29-30, 136): hour>=22 or hour<8.
_QUIET_START = 22
_QUIET_END = 8

# Alert-rate that maps to "full" noradrenaline drive (criticals/hr). Above this
# the drive saturates — the clamp does the rest.
_ALERT_RATE_FULL = 20.0


def _clamp01(x: float) -> float:
    if x != x:  # NaN guard — a NaN signal must never poison a level
        return 0.5
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _clamp(x: float, lo: float, hi: float) -> float:
    if x != x:
        return 0.0
    return lo if x < lo else hi if x > hi else x


@dataclass(frozen=True)
class Regulator:
    """One hormone's homeostatic parameters: leak gain + homeostatic setpoint.

    leak (α) is the negative-feedback gain toward `setpoint`. 0 < leak < 1 is
    the contraction condition — slower hormones get a smaller leak (longer
    memory / slower decay), matching their biology:
      • cortisol/serotonin — slow, stable baselines (small leak)
      • noradrenaline — fast acute spike that decays quickly (large leak)
    """

    name: str
    leak: float
    setpoint: float

    # Decay half-life intuition: with leak α, distance halves every
    # ln(2)/(-ln(1-α)) ticks. At a 60s tick: leak 0.05 ≈ 13.5 min half-life
    # (slow cortisol), leak 0.35 ≈ 1.6 min (fast noradrenaline).
    _TABLE: ClassVar[dict[str, tuple[float, float]]] = {
        "dopamine": (0.10, 0.30),  # baseline modest drive
        "cortisol": (0.05, 0.20),  # slow, low resting stress
        "noradrenaline": (0.35, 0.10),  # fast spike + fast decay, low rest
        "oxytocin": (0.08, 0.50),  # cohesion rests mid (neither herd nor pure red-team)
        "melatonin": (0.15, 0.20),  # circadian, moderate speed
        "serotonin": (0.04, 0.60),  # slowest, confident stable baseline
    }

    @classmethod
    def for_hormone(cls, name: str) -> Regulator:
        leak, setpoint = cls._TABLE[name]
        return cls(name=name, leak=leak, setpoint=setpoint)

    def step(self, level: float, drive: float) -> float:
        """One homeostatic update. Drive is clamped before it can act."""
        d = _clamp(drive, -DRIVE_MAX, DRIVE_MAX)
        nxt = level + self.leak * (self.setpoint - level) + d
        return _clamp01(nxt)

    def drive_toward(self, target: float) -> float:
        """The constant drive whose FIXED POINT is `target`.

        Closed form: the leaky integrator h ← (1−α)h + α·setpoint + d has fixed
        point h* = setpoint + d/α. So to settle at `target`, drive = (target −
        setpoint)·α. This keeps the signal→hormone map calibrated: a tier that
        means "0.8 stress" actually settles cortisol at 0.8, monotonically
        separable from the 0.5 tier — not slammed to the rail by an over-hot
        gain (the bug TDD caught: a ×3 gain collapsed every tier to 1.0)."""
        return (target - self.setpoint) * self.leak


@dataclass(frozen=True)
class Signals:
    """The REAL local, non-sensitive inputs the gland reads.

    All optional/None-able: a None means "honestly blind on this axis" — the
    gland holds the level toward setpoint rather than fabricating a value
    (no-PII, no-secrets; these are pure system telemetry).
    """

    headroom_mb: int | None = None  # castellan /status headroom_mb (cortisol)
    alert_rate_1h: int | None = None  # Force-Flow critical+p0 count last hour (noradrenaline)
    hour: int | None = None  # local hour 0..23 (melatonin/circadian)
    creative_mode: bool = False  # explicit task-mode lever (dopamine↑, cortisol↓)

    # ── signal → raw drive maps (each returns a small per-tick nudge) ──

    def cortisol_drive(self) -> float:
        """Memory pressure → cortisol. Mapped off castellan's own tiers."""
        if self.headroom_mb is None:
            return 0.0  # blind → no drive, leak holds at setpoint
        h = self.headroom_mb
        if h < _CASTELLAN_CATASTROPHIC_MB:
            target = 1.0
        elif h < _CASTELLAN_CRITICAL_MB:
            target = 0.8
        elif h < _CASTELLAN_WARN_MB:
            target = 0.5
        else:
            target = 0.0
        # alert rate adds sustained stress on top of memory pressure
        if self.alert_rate_1h:
            target = min(1.0, target + 0.02 * self.alert_rate_1h)
        # drive whose fixed point IS the tier target (calibrated P-term)
        return Regulator.for_hormone("cortisol").drive_toward(target)

    def noradrenaline_drive(self) -> float:
        """Acute alert rate → noradrenaline spike."""
        if self.alert_rate_1h is None:
            return 0.0
        intensity = min(1.0, self.alert_rate_1h / _ALERT_RATE_FULL)
        return Regulator.for_hormone("noradrenaline").drive_toward(intensity)

    def melatonin_drive(self) -> float:
        """Time-of-day → melatonin (circadian). Smooth cosine baseline, peak
        at ~03:00, trough at ~15:00 — not a hard quiet-hours step."""
        if self.hour is None:
            return 0.0
        import math

        # cosine peaking at hour 3 (deepest night), trough at hour 15
        phase = (self.hour - 3) / 24.0 * 2 * math.pi
        target = 0.5 * (1 + math.cos(phase))  # 1.0 at 03:00, 0.0 at 15:00
        return Regulator.for_hormone("melatonin").drive_toward(target)

    def dopamine_drive(self, cortisol_level: float) -> float:
        """Creative mode → dopamine drive, ANTAGONIZED by current cortisol.

        Cross-coupling cortisol ⊣ dopamine: the more stressed the panel, the
        less the creative push can raise dopamine. Negative-only coupling."""
        reg = Regulator.for_hormone("dopamine")
        target = 0.9 if self.creative_mode else reg.setpoint
        base = reg.drive_toward(target)
        # antagonism: cortisol subtracts from dopamine drive (never adds).
        # Scaled by leak so it shares the drive's units; a full cortisol pulls
        # the dopamine fixed point down by ~0.6 (target − antagonism/leak).
        antagonism = cortisol_level * reg.leak * 0.6
        return base - antagonism

    def cortisol_relief(self) -> float:
        """Creative mode also gently LOWERS cortisol (calm-to-create)."""
        if not self.creative_mode:
            return 0.0
        reg = Regulator.for_hormone("cortisol")
        return -0.5 * reg.leak

    def oxytocin_drive(self) -> float:
        """Creative mode lowers oxytocin (more independent / red-team diversity);
        otherwise rests at setpoint."""
        if self.creative_mode:
            reg = Regulator.for_hormone("oxytocin")
            return -0.5 * reg.leak
        return 0.0


@dataclass(frozen=True)
class Panel:
    """The hormone panel — six scalar levels in [0,1]. Immutable snapshot."""

    dopamine: float
    cortisol: float
    noradrenaline: float
    oxytocin: float
    melatonin: float
    serotonin: float

    @classmethod
    def neutral(cls) -> Panel:
        """The homeostatic baseline — every hormone at its setpoint.

        This is the OFF-BY-DEFAULT panel: a receptor reading it produces no
        knob changes, so a freshly-started gland (or one with no signals)
        leaves every seat byte-identical to today."""
        return cls(**{h: Regulator.for_hormone(h).setpoint for h in HORMONES})

    def to_wire(self) -> dict[str, float]:
        """The broadcast wire shape: served on /panel + embedded in samskara."""
        return {h: round(getattr(self, h), 4) for h in HORMONES}

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> Panel:
        """Parse a panel off the wire, tolerating missing/garbage keys by
        falling back to the neutral setpoint (fail-soft, like Mood::calm())."""
        vals: dict[str, float] = {}
        for h in HORMONES:
            v: Any = wire.get(h)
            try:
                vals[h] = _clamp01(float(v))  # None/garbage falls to the except
            except (TypeError, ValueError):
                vals[h] = Regulator.for_hormone(h).setpoint
        return cls(**vals)


def step_panel(panel: Panel, signals: Signals) -> Panel:
    """Advance the whole panel ONE homeostatic tick under the given signals.

    Order matters for the cross-coupling: cortisol is updated first so the
    dopamine antagonism reads the FRESH cortisol level (the suppressor leads)."""
    # cortisol first (it's the antagonist)
    cort = Regulator.for_hormone("cortisol").step(
        panel.cortisol, signals.cortisol_drive() + signals.cortisol_relief()
    )
    dopa = Regulator.for_hormone("dopamine").step(panel.dopamine, signals.dopamine_drive(cort))
    nora = Regulator.for_hormone("noradrenaline").step(
        panel.noradrenaline, signals.noradrenaline_drive()
    )
    oxy = Regulator.for_hormone("oxytocin").step(panel.oxytocin, signals.oxytocin_drive())
    mela = Regulator.for_hormone("melatonin").step(panel.melatonin, signals.melatonin_drive())
    # serotonin: slow baseline, only gently nudged down by sustained cortisol
    sero_reg = Regulator.for_hormone("serotonin")
    sero = sero_reg.step(panel.serotonin, -0.3 * sero_reg.leak * cort)
    return Panel(
        dopamine=dopa,
        cortisol=cort,
        noradrenaline=nora,
        oxytocin=oxy,
        melatonin=mela,
        serotonin=sero,
    )


def settle(signals: Signals, iterations: int = 400, start: Panel | None = None) -> Panel:
    """Run the regulator to its fixed point under fixed `signals`.

    Because the map is a contraction, this CONVERGES (it does not diverge);
    `iterations` is a generous upper bound — the homeostasis tests assert the
    contraction holds regardless of the count. The live daemon does NOT settle;
    it steps once per tick so the panel lags the signals (slow modulation)."""
    panel = start if start is not None else Panel.neutral()
    for _ in range(iterations):
        panel = step_panel(panel, signals)
    return panel


# ───────────────────────── bloodstream serialization ─────────────────────


def samskara_record(panel: Panel) -> dict[str, Any]:
    """A timeseries record for the chitti samskara journal.

    Shape MATCHES the live samskara schema (ts/service/pattern/action/success)
    so existing readers (launchd-health-sentinel, triage, the watchdog picker)
    parse it. The panel scalars ride in `action` as a compact k:v string —
    derived from a live row, not invented."""
    action = " ".join(f"{h}:{round(getattr(panel, h), 3)}" for h in HORMONES)
    return {
        "ts": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "service": "endocrine",
        "pattern": "hormone-panel",
        "action": action,
        "success": True,  # a published panel is a healthy event (ambient tier)
    }


def is_pathological(panel: Panel) -> tuple[bool, str]:
    """The ONLY thing the gland-sentinel pages on (via alert-confirm.sh,
    probe-twice + cooldown): a structurally impossible / stuck-storm state.

    The regulator CANNOT produce an out-of-range level (the math forbids it),
    so a True here means the INPUTS are stuck or the gland is mis-running —
    a real, page-worthy organ fault. Returns (is_pathological, reason)."""
    for h in HORMONES:
        v = getattr(panel, h)
        if v != v or v < 0.0 or v > 1.0:
            return True, f"{h} out of [0,1]: {v} (regulator invariant violated)"
    # stuck-high cortisol (the canonical 'hormone storm') — only meaningful when
    # SUSTAINED, which the sentinel's confirm-twice + hold-window enforces.
    if panel.cortisol >= 0.97:
        return True, f"cortisol pinned high ({panel.cortisol:.2f}) — stuck stress axis"
    return False, ""
