"""``sanctum matrix`` — the digital rain, as a treat.

A terminal homage to the 1999 original: bright glyph heads falling down
columns, green trails fading behind them, the occasional Sanctum ◆
hiding in the rain. Pure column/frame math lives here (unit-tested);
the Live loop is gated on a real TTY exactly like the launch banner —
pipes and NO_COLOR get a polite refusal, not frames.
"""

from __future__ import annotations

import random  # noqa: TC003 - random.Random used at runtime for function signatures
from dataclasses import dataclass

# Phosphor palette: head glyph → bright trail → the deep.
_HEAD = (204, 255, 204)
_TRAIL_BRIGHT = (0, 255, 65)
_TRAIL_DEEP = (0, 80, 24)


@dataclass(frozen=True)
class ColumnState:
    """One column of rain: a bright head dragging a fading trail."""

    head: int  # row of the head glyph; negative while sliding in from above
    trail: int  # glyphs in the trail behind the head
    period: int  # frames per one-row fall (1 = falls every frame)
    phase: int = 0  # frame counter modulo period


def spawn_column(height: int, rng: random.Random) -> ColumnState:
    """A fresh column: head above the screen so it slides in, not pops in."""
    return ColumnState(
        head=-rng.randrange(height),
        trail=rng.randrange(3, max(4, height)),
        period=rng.choice((1, 1, 2, 3)),
    )


def step_column(col: ColumnState, height: int, rng: random.Random) -> ColumnState:
    """Advance one frame; respawn once the whole trail has drained off-screen."""
    phase = (col.phase + 1) % col.period
    if phase != 0:
        return ColumnState(col.head, col.trail, col.period, phase)
    if col.head - col.trail > height:
        return spawn_column(height, rng)
    return ColumnState(col.head + 1, col.trail, col.period, phase)
