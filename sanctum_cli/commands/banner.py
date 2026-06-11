"""Sanctum Council launch banner — a kyber-diamond that materializes.

The diamond reveals from its apex, a shimmer band sweeps the facets, the
SANCTUM wordmark settles in, and the five council seats light up in their
Jedi colours. Pure frame/colour math lives here (unit-tested); the Live
animation is gated on a real TTY (pipes, NO_COLOR, and SANCTUM_NO_ANIM get
the instant static banner instead).

Palette is the Sanctum brand (sanctum-docs custom.css):
  amber #d4952e · amber-bright #f0d4a0 · holocron-blue #4a7a9b
"""

from __future__ import annotations

import os
import time

from rich.align import Align
from rich.color import Color
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.style import Style
from rich.text import Text

DIAMOND_H = 5  # half-height; full diamond is 2*H-1 = 9 rows

# Brand stops as RGB (top → mid → bottom of the gem).
_AMBER_BRIGHT = (240, 212, 160)
_AMBER = (212, 149, 46)
_HOLOCRON = (74, 122, 155)

# Seats in lighting order, with their REPL colours (mirrors council.SEATS).
_SEATS: tuple[tuple[str, str], ...] = (
    ("Yoda", "green"),
    ("Windu", "magenta"),
    ("Qui-Gon", "cyan"),
    ("Ki-Adi-Mundi", "yellow"),
    ("Cilghal", "blue"),
    ("Jocasta", "bright_white"),
    ("Mon Mothma", "bright_blue"),
)


# ── Pure geometry ─────────────────────────────────────────────────────


def diamond_lines(h: int) -> list[str]:
    """A faceted kyber diamond, 2*h-1 rows, apex-to-apex symmetric."""
    lines: list[str] = []
    for i in range(h):
        pad = " " * (h - 1 - i)
        body = "◢" + "█" * (2 * i) + "◣" if i else "◆"
        lines.append(pad + body)
    for j in range(h - 1):
        k = h - 2 - j
        pad = " " * (h - 1 - k)
        body = "◥" + "█" * (2 * k) + "◤" if k else "◆"
        lines.append(pad + body)
    return lines


def reveal_frames(h: int) -> list[list[str]]:
    """Frames that draw the diamond from the apex down, constant canvas
    height so the terminal never jitters."""
    full = diamond_lines(h)
    total = len(full)
    frames: list[list[str]] = []
    for visible in range(1, total + 1):
        frame = [full[r] if r < visible else "" for r in range(total)]
        frames.append(frame)
    return frames


def gradient_rgb(t: float) -> tuple[int, int, int]:
    """Vertical gem gradient: bright apex → amber middle → holocron base."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        a, b, local = _AMBER_BRIGHT, _AMBER, t / 0.5
    else:
        a, b, local = _AMBER, _HOLOCRON, (t - 0.5) / 0.5
    return tuple(round(a[i] + (b[i] - a[i]) * local) for i in range(3))  # type: ignore[return-value]


def shimmer_row(phase: float, rows: int) -> int:
    """Which row the bright shimmer band sits on for a given 0..1 phase."""
    phase = max(0.0, min(1.0, phase))
    return min(rows - 1, int(phase * (rows - 1) + 0.5))


# ── Styling helpers ───────────────────────────────────────────────────


def _diamond_text(lines: list[str], rows: int, shimmer: int | None) -> Text:
    out = Text(justify="center")
    drawn = [ln for ln in lines if ln.strip()]
    n = max(1, len(drawn))
    di = 0
    for ln in lines:
        if not ln.strip():
            out.append("\n")
            continue
        t = di / max(1, n - 1)
        r, g, b = gradient_rgb(t)
        if shimmer is not None and di == shimmer:
            r, g, b = _AMBER_BRIGHT  # the highlight band
            out.append(ln, Style(color=Color.from_rgb(r, g, b), bold=True))
        else:
            out.append(ln, Style(color=Color.from_rgb(r, g, b)))
        out.append("\n")
        di += 1
    return out


def _wordmark() -> Text:
    t = Text(justify="center")
    t.append("S A N C T U M\n", Style(color=Color.from_rgb(*_AMBER_BRIGHT), bold=True))
    t.append("· COUNCIL CHAMBER ·", Style(color=Color.from_rgb(*_AMBER), italic=True))
    return t


def _seat_line(lit: int) -> Text:
    """The five seats; the first `lit` are illuminated, the rest dim."""
    t = Text(justify="center")
    for idx, (label, colour) in enumerate(_SEATS):
        glyph = "◆" if idx < lit else "◇"
        style = Style(color=colour, bold=True) if idx < lit else Style(color="grey39")
        t.append(glyph + " ", style)
        t.append(label + "   ", style if idx < lit else Style(color="grey39"))
    return t


def static_banner_text() -> str:
    """Plain-text banner for pipes / NO_COLOR — same content, no motion."""
    lines = diamond_lines(DIAMOND_H)
    seats = "   ".join(f"◆ {label}" for label, _ in _SEATS)
    return "\n".join(lines) + "\n\nS A N C T U M  ·  COUNCIL CHAMBER\n" + seats


# ── Policy ────────────────────────────────────────────────────────────


def should_animate(is_tty: bool) -> bool:
    if not is_tty:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("SANCTUM_NO_ANIM"):
        return False
    return True


# ── Render ────────────────────────────────────────────────────────────


def _compose(diamond: Text, lit_seats: int, show_word: bool) -> Group:
    parts: list[RenderableType] = [Align.center(diamond)]
    if show_word:
        parts.append(Align.center(_wordmark()))
    else:
        parts.append(Text("\n\n"))
    parts.append(Text(""))
    parts.append(Align.center(_seat_line(lit_seats)))
    return Group(*parts)


def render_banner(console: Console, *, animate: bool | None = None) -> None:
    """Draw the launch banner. Animated on a TTY, instant otherwise."""
    full = diamond_lines(DIAMOND_H)
    rows = len(full)
    if animate is None:
        animate = should_animate(console.is_terminal)

    if not animate:
        console.print()
        console.print(_compose(_diamond_text(full, rows, None), len(_SEATS), True))
        console.print()
        return

    console.print()
    with Live(console=console, refresh_per_second=60, transient=False) as live:
        # 1) materialize from the apex
        for frame in reveal_frames(DIAMOND_H):
            live.update(_compose(_diamond_text(frame, rows, None), 0, False))
            time.sleep(0.045)
        # 2) shimmer sweep down the facets
        steps = 14
        for s in range(steps):
            band = shimmer_row(s / (steps - 1), rows)
            live.update(_compose(_diamond_text(full, rows, band), 0, False))
            time.sleep(0.03)
        # 3) wordmark settles, seats ignite one by one
        for lit in range(len(_SEATS) + 1):
            live.update(_compose(_diamond_text(full, rows, None), lit, True))
            time.sleep(0.11)
    console.print()
