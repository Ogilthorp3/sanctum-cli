"""``sanctum matrix`` — the digital rain.

Tests cover the PURE parts: column spawn/step mechanics, the glyph
charset, trail-fade math, and exact frame geometry. The Live loop is
thin glue gated by the same should_animate policy the banner already
proves under test.
"""

from __future__ import annotations

import random

from sanctum_cli.commands import matrix as m


class TestColumns:
    def test_spawn_starts_above_screen_with_sane_trail(self) -> None:
        rng = random.Random(42)
        for _ in range(50):
            col = m.spawn_column(24, rng)
            assert -24 <= col.head < 0, "heads slide in from above, never pop mid-screen"
            assert 3 <= col.trail < 24
            assert col.period >= 1
            assert 0 <= col.phase < col.period, "spawn phases stagger so cohorts don't strobe"

    def test_head_advances_one_row_per_period(self) -> None:
        rng = random.Random(1)
        col = m.ColumnState(head=5, trail=4, period=1)
        assert m.step_column(col, 24, rng).head == 6

    def test_slow_column_waits_its_period(self) -> None:
        rng = random.Random(1)
        col = m.ColumnState(head=5, trail=4, period=2, phase=0)
        waiting = m.step_column(col, 24, rng)  # off-beat frame: no fall
        assert (waiting.head, waiting.phase) == (5, 1)
        falling = m.step_column(waiting, 24, rng)  # on-beat frame: falls
        assert (falling.head, falling.phase) == (6, 0)

    def test_respawns_after_draining_below_screen(self) -> None:
        rng = random.Random(7)
        col = m.ColumnState(head=40, trail=4, period=1)  # trail fully past height=24
        assert m.step_column(col, 24, rng).head <= 0

    def test_trail_drains_never_truncates(self) -> None:
        rng = random.Random(7)
        col = m.ColumnState(head=25, trail=10, period=1)  # head off-bottom, trail still visible
        assert m.step_column(col, 24, rng).head == 26, "trail drains, never truncates"


class TestGlyphs:
    def test_glyphs_come_from_the_declared_charset(self) -> None:
        rng = random.Random(3)
        seen = {m.pick_glyph(rng) for _ in range(2000)}
        assert seen <= set(m.GLYPHS) | {m.GEM}

    def test_the_gem_hides_in_the_rain(self) -> None:
        rng = random.Random(3)
        assert any(m.pick_glyph(rng) == m.GEM for _ in range(2000))

    def test_charset_is_single_cell_wide(self) -> None:
        # the frame grid is cell math — a double-width glyph would shear it
        from rich.cells import cell_len

        assert all(cell_len(g) == 1 for g in [*m.GLYPHS, m.GEM])


class TestFrame:
    def test_frame_is_exactly_height_by_width(self) -> None:
        rng = random.Random(5)
        cols = [m.spawn_column(10, rng) for _ in range(8)]
        frame = m.compose_frame(cols, width=8, height=10, rng=rng)
        lines = frame.plain.split("\n")
        assert len(lines) == 10
        assert all(len(line) == 8 for line in lines)

    def test_trail_fades_toward_the_deep(self) -> None:
        near = m.trail_rgb(1, 10)
        far = m.trail_rgb(10, 10)
        assert near[1] > far[1], "green channel must dim with distance from the head"
