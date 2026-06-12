"""``sanctum matrix`` — the digital rain, as a treat.

A terminal homage to the 1999 original: bright glyph heads falling down
columns, green trails fading behind them, the occasional Sanctum ◆
hiding in the rain. Pure column/frame math lives here (unit-tested);
the Live loop is gated on a real TTY exactly like the launch banner —
pipes and NO_COLOR get a polite refusal, not frames.
"""

from __future__ import annotations

import random  # noqa: TC003 - the Task 5 command shell constructs random.Random() at runtime
from dataclasses import dataclass, replace

from rich.color import Color
from rich.style import Style
from rich.text import Text

# Phosphor palette: head glyph → bright trail → the deep.
_HEAD = (204, 255, 204)
_TRAIL_BRIGHT = (0, 255, 65)
_TRAIL_DEEP = (0, 80, 24)

# Charset: half-width katakana (single-cell — full-width would shear the
# grid) plus digits, with a rare Sanctum gem hiding in the rain.
GLYPHS = [chr(cp) for cp in range(0xFF66, 0xFF9E)] + list("0123456789")
GEM = "◆"
GEM_RARITY = 200  # ~one gem per 200 glyphs


@dataclass(frozen=True)
class ColumnState:
    """One column of rain: a bright head dragging a fading trail."""

    head: int  # row of the head glyph; negative while sliding in from above
    trail: int  # glyphs in the trail behind the head
    period: int  # frames per one-row fall (1 = falls every frame)
    phase: int = 0  # frame counter modulo period


def pick_glyph(rng: random.Random) -> str:
    """One rain glyph — usually katakana/digit, rarely the gem."""
    if rng.randrange(GEM_RARITY) == 0:
        return GEM
    return rng.choice(GLYPHS)


def trail_rgb(distance: int, trail: int) -> tuple[int, int, int]:
    """Linear fade from bright phosphor to the deep across the trail."""
    t = distance / max(1, trail)
    return tuple(  # type: ignore[return-value]
        round(_TRAIL_BRIGHT[i] + (_TRAIL_DEEP[i] - _TRAIL_BRIGHT[i]) * t) for i in range(3)
    )


def compose_frame(columns: list[ColumnState], width: int, height: int, rng: random.Random) -> Text:
    """One full screen of rain: exactly `height` rows by `width` cells."""
    frame = Text()
    for row in range(height):
        for x in range(width):
            col = columns[x]
            distance = col.head - row
            if distance == 0:
                frame.append(
                    pick_glyph(rng),
                    Style(color=Color.from_rgb(*_HEAD), bold=True),
                )
            elif 0 < distance <= col.trail:
                frame.append(
                    pick_glyph(rng),
                    Style(color=Color.from_rgb(*trail_rgb(distance, col.trail))),
                )
            else:
                frame.append(" ")
        if row < height - 1:
            frame.append("\n")
    return frame


def spawn_column(height: int, rng: random.Random) -> ColumnState:
    """A fresh column: head above the screen so it slides in, not pops in."""
    period = rng.choice((1, 1, 2, 3))
    return ColumnState(
        head=-rng.randrange(1, height + 1),
        trail=rng.randrange(3, max(4, height)),
        period=period,
        phase=rng.randrange(period),
    )


def step_column(col: ColumnState, height: int, rng: random.Random) -> ColumnState:
    """Advance one frame; respawn once the whole trail has drained off-screen."""
    phase = (col.phase + 1) % col.period
    if phase != 0:
        return replace(col, phase=phase)
    if col.head - col.trail > height:
        return spawn_column(height, rng)
    return replace(col, head=col.head + 1, phase=phase)
