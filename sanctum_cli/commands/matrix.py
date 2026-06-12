"""``sanctum matrix`` — the digital rain, as a treat.

A terminal homage to the 1999 original: bright glyph heads falling down
columns, green trails fading behind them, the occasional Sanctum ◆
hiding in the rain. Pure column/frame math lives here (unit-tested);
the Live loop is gated on a real TTY exactly like the launch banner —
pipes and NO_COLOR get a polite refusal, not frames.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, replace

import typer
from rich.color import Color
from rich.console import Console
from rich.live import Live
from rich.style import Style
from rich.text import Text

from sanctum_cli.commands.banner import should_animate

console = Console()

# Phosphor palette: head glyph → bright trail → the deep.
_HEAD = (204, 255, 204)
_TRAIL_BRIGHT = (0, 255, 65)
_TRAIL_DEEP = (0, 80, 24)

# Charset: half-width katakana (single-cell — full-width would shear the
# grid) plus digits, with a rare Sanctum gem hiding in the rain.
GLYPHS = [chr(cp) for cp in range(0xFF66, 0xFF9E)] + list("0123456789")
# U+25C6 is East-Asian-Width Ambiguous — rich counts it as 1 cell, but a
# terminal configured to treat ambiguous as wide would render 2 cells and
# shear the grid. We accept this bet because no good single-cell diamond exists.
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


def matrix_command() -> None:
    """``sanctum matrix`` — there is no spoon, only launchd."""
    if not should_animate(console.is_terminal):
        console.print("[red]The Matrix needs a real terminal[/] — not a pipe.")
        raise typer.Exit(1)
    rng = random.Random()
    columns: list[ColumnState] = []
    try:
        with Live(console=console, screen=True, refresh_per_second=20, transient=True) as live:
            while True:
                # re-read the size every frame so a resize reshapes the rain
                # instead of crashing it; clamp so a pathological 0-row
                # terminal can't blow up spawn_column. compose_frame indexes
                # columns[x] for x < width, so the reconcile below MUST keep
                # len(columns) >= width — that ordering is a hard contract.
                width = max(1, console.size.width)
                height = max(1, console.size.height)
                if len(columns) < width:
                    columns.extend(spawn_column(height, rng) for _ in range(width - len(columns)))
                elif len(columns) > width:
                    columns = columns[:width]
                columns = [step_column(c, height, rng) for c in columns]
                live.update(compose_frame(columns, width, height, rng))
                time.sleep(0.05)
    except KeyboardInterrupt:
        # Live(screen=True) restores the terminal on context exit; the rain
        # leaves no residue, only the parting line.
        pass
    console.print("[dim]Wake up, Neo… the chamber awaits.[/]")
