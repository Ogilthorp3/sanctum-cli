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
            assert -24 <= col.head <= 0, "heads slide in from above, never pop mid-screen"
            assert 3 <= col.trail < 24
            assert col.period >= 1

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
