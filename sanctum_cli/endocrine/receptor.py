"""RECEPTORS — the OPT-IN, OFF-BY-DEFAULT translation from a hormone Panel to a
council seat's concrete knobs (sampling, prompt framing, model-diversity).

Design contract (mirrors chitti_client.rs's fail-soft Mood read):
  • A NEUTRAL panel (every hormone at setpoint) → returns NOTHING. Empty
    sampling delta, empty framing clause, baseline diversity. So a seat that
    subscribes but sits at homeostasis is byte-identical to today.
  • An ABSENT panel (None — gland down / not subscribed) → same: nothing.
  • Only a panel that has DEPARTED from neutral changes a knob, and only by a
    bounded amount. There is no path where the receptor itself runs away.
  • SUBSCRIPTION-FIRST: diversity engagement NEVER includes a metered seat.

These are pure functions — a seat calls them and merges the result into its
own payload. No global state, no side effects, no network. The seat decides
whether to subscribe (the off-by-default switch lives at the call site).
"""

from __future__ import annotations

from typing import Protocol

from .gland import HORMONES, Panel, Regulator

# proxyd forwards temperature/top_p transparently (translate.rs:62). The council
# REPL today sends NO temperature, so proxyd/backends apply their own default.
# We treat 0.7 as the neutral anchor: a neutral panel emits no temperature key
# at all (true off-by-default), and departures move RELATIVE to this anchor.
BASELINE_TEMPERATURE = 0.7
BASELINE_TOP_P = 0.95

# How far a fully-saturated hormone can move temperature off baseline. Bounded
# so even an extreme panel stays in proxyd's forwardable 0..2 band.
_TEMP_DOPAMINE_GAIN = 0.6  # full dopamine → +0.6 (divergent)
_TEMP_CORTISOL_GAIN = 0.5  # full cortisol → −0.5 (convergent/focused)
_TEMP_MELATONIN_GAIN = 0.1  # night → slightly terser/cooler

# A panel counts as "neutral" (→ no-op) if every hormone is within this of its
# setpoint. Keeps tiny numerical drift from accidentally flipping the switch.
_NEUTRAL_EPS = 1e-3


class SeatLike(Protocol):
    """Only the fields the receptor reads. Kept minimal so any seat-shaped
    object (the real Seat dataclass, a test double) satisfies it."""

    model: str
    persona: str


def _is_neutral(panel: Panel) -> bool:
    return all(
        abs(getattr(panel, h) - Regulator.for_hormone(h).setpoint) <= _NEUTRAL_EPS for h in HORMONES
    )


def sampling_for(seat: SeatLike, panel: Panel | None) -> dict[str, float]:  # noqa: ARG001
    """Translate a panel into a sampling delta to merge into a seat's payload.

    ``seat`` is part of the receptor contract (the call site hands the seat that
    is being modulated) and is reserved for future per-seat tuning — e.g. a
    code seat capping temperature lower than a brainstorming seat. It is
    deliberately unused in this first version: the panel alone sets the knobs.

    Returns {} for an absent or neutral panel (OFF BY DEFAULT). Otherwise
    returns {"temperature": t, "top_p": p} with t bounded to proxyd's range.

    Effective temperature = baseline + dopamine·gain − cortisol·gain − night·gain
    (dopamine fuels divergence/heat; cortisol & melatonin cool/converge)."""
    if panel is None or _is_neutral(panel):
        return {}

    temp = (
        BASELINE_TEMPERATURE
        + panel.dopamine * _TEMP_DOPAMINE_GAIN
        - panel.cortisol * _TEMP_CORTISOL_GAIN
        - panel.melatonin * _TEMP_MELATONIN_GAIN
    )
    temp = max(0.0, min(2.0, temp))

    # top_p widens with dopamine (more of the distribution in play when
    # exploring), narrows with cortisol (focus on the high-probability mass).
    top_p = BASELINE_TOP_P + 0.04 * panel.dopamine - 0.10 * panel.cortisol
    top_p = max(0.1, min(1.0, top_p))

    return {"temperature": round(temp, 3), "top_p": round(top_p, 3)}


def framing_clause(panel: Panel | None) -> str:
    """A short disposition clause to APPEND to a seat's system prompt.

    Empty string for absent/neutral panel (off by default). Divergent under
    high dopamine, convergent under high cortisol. The clause is additive — it
    never rewrites the seat's persona, only tilts its framing."""
    if panel is None or _is_neutral(panel):
        return ""

    # decide by the dopamine−cortisol axis (the creativity axis)
    axis = panel.dopamine - panel.cortisol
    parts: list[str] = []
    if axis >= 0.25:
        parts.append(
            "Disposition: exploratory. Favour divergent framing — propose "
            "several distinct angles, including a wild one; defer convergence."
        )
    elif axis <= -0.25:
        parts.append(
            "Disposition: focused. Favour convergent reasoning — conserve, "
            "commit to the strongest single line, avoid speculative tangents."
        )
    if panel.noradrenaline >= 0.6:
        parts.append("Acute arousal: be sharp and fast; lead with the answer.")
    if panel.melatonin >= 0.6:
        parts.append("Quiet hours: be terse; defer anything non-urgent.")
    if panel.oxytocin <= 0.25:
        parts.append("Low cohesion: red-team freely; disagreement is welcome.")
    return " ".join(parts)


def diversity_seats(
    panel: Panel | None,
    all_seats: dict[str, str],
    metered: set[str] | None = None,
) -> list[str]:
    """Which seats to ENGAGE for a fan-out, given the panel.

    "neurodiversity paramount" made DYNAMIC: high dopamine (creative mode)
    engages the MAX set of seats for divergence; neutral/absent engages a
    conservative core. SUBSCRIPTION-FIRST is absolute: a metered seat is
    NEVER engaged regardless of hormone state.

    all_seats: {seat_id: model_name}. metered: ids that route to a paid
    provider (OpenRouter) — always excluded."""
    metered = metered or set()
    eligible = [s for s in all_seats if s not in metered]

    if panel is None or _is_neutral(panel):
        # conservative baseline: the core subscription/local seats only.
        # (Engage the first half — a stable, small council.)
        return eligible[: max(1, len(eligible) // 2)]

    if panel.dopamine - panel.cortisol >= 0.25:
        # creative: engage EVERY non-metered seat (max diversity)
        return eligible

    # mild departure: engage a wider-than-baseline but not-max set
    return eligible[: max(1, (len(eligible) * 3) // 4)]
