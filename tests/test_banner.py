"""Sanctum council banner — the kyber-diamond launch animation.

Tests cover the PURE parts (the diamond art geometry, the vertical reveal
frame sequence, the gradient stop math, the animate-or-not policy) AND the
rendered contract with Rich: the pure geometry can be perfect while the
renderer skews it (the 2026-06-11 leaning-diamond bug — justify="center"
re-centered each line on top of Align.center). The Live animation itself is
exercised only through the policy gate (a non-TTY / NO_COLOR /
SANCTUM_NO_ANIM env must never animate).
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from sanctum_cli.commands import banner as b


class TestDiamondArt:
    def test_diamond_is_symmetric_and_pointed(self) -> None:
        art = b.diamond_lines(b.DIAMOND_H)
        # top point + widening, a single widest seam, then narrowing to a point
        assert art[0].strip() == "◆"
        assert art[-1].strip() == "◆"
        widths = [len(ln.strip()) for ln in art]
        assert widths == sorted(widths[: len(widths) // 2 + 1]) + sorted(
            widths[len(widths) // 2 + 1 :], reverse=True
        )
        # vertically symmetric in stripped width
        assert widths == widths[::-1]

    def test_height_is_odd_so_it_has_one_apex(self) -> None:
        assert len(b.diamond_lines(b.DIAMOND_H)) % 2 == 1


class TestRevealFrames:
    def test_reveal_grows_then_completes(self) -> None:
        frames = b.reveal_frames(b.DIAMOND_H)
        # first frame shows only the apex; last frame is the full diamond
        first_visible = [ln for ln in frames[0] if ln.strip()]
        assert len(first_visible) == 1
        full = b.diamond_lines(b.DIAMOND_H)
        assert [ln.rstrip() for ln in frames[-1]] == [ln.rstrip() for ln in full]
        # every frame keeps the canvas a constant height (no jitter)
        assert all(len(f) == len(full) for f in frames)

    def test_reveal_is_monotonic(self) -> None:
        frames = b.reveal_frames(b.DIAMOND_H)
        prev = -1
        for f in frames:
            vis = sum(1 for ln in f if ln.strip())
            assert vis >= prev, "reveal must never un-draw a row"
            prev = vis


class TestGradient:
    def test_stops_interpolate_within_palette(self) -> None:
        top = b.gradient_rgb(0.0)
        mid = b.gradient_rgb(0.5)
        bot = b.gradient_rgb(1.0)
        for c in (top, mid, bot):
            assert all(0 <= v <= 255 for v in c)
        # the three stops are visibly distinct
        assert top != mid and mid != bot

    def test_shimmer_band_moves(self) -> None:
        a = b.shimmer_row(0.0, rows=9)
        z = b.shimmer_row(1.0, rows=9)
        assert 0 <= a < 9 and 0 <= z < 9
        assert a != z


class TestAnimationPolicy:
    def test_no_animation_without_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SANCTUM_NO_ANIM", raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert b.should_animate(is_tty=False) is False

    def test_no_animation_when_no_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        assert b.should_animate(is_tty=True) is False

    def test_opt_out_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("SANCTUM_NO_ANIM", "1")
        assert b.should_animate(is_tty=True) is False

    def test_animates_for_a_human(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SANCTUM_NO_ANIM", raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert b.should_animate(is_tty=True) is True


class TestRenderedGeometry:
    """Contract test at the Rich boundary — geometry as it actually lands
    on the terminal, not as diamond_lines() intends it. Catches any
    double-centering between Text justify and Align.center."""

    @pytest.mark.parametrize("width", [60, 61])  # even + odd console widths
    def test_rendered_facet_rows_share_one_center(self, width: int) -> None:
        buf = StringIO()
        console = Console(file=buf, width=width, force_terminal=False, no_color=True)
        b.render_banner(console, animate=False)
        # diamond rows are the lines made purely of gem glyphs (the seat
        # line also carries ◆ but has labels, so it self-excludes)
        gem_rows = [
            ln.rstrip()
            for ln in buf.getvalue().splitlines()
            if ln.strip() and set(ln.strip()) <= set("◆◢◣◥◤█")
        ]
        assert len(gem_rows) == 2 * b.DIAMOND_H - 1
        # every row — ◆ apexes included — must share ONE exact centre
        # column (the art is all-odd-width precisely so this holds)
        centers = {(len(row) - len(row.lstrip())) + len(row.strip()) / 2 for row in gem_rows}
        assert len(centers) == 1, f"diamond leans: centers={sorted(centers)}"

    @pytest.mark.parametrize("width", [110, 153])  # wide enough for the seat line
    def test_whole_banner_shares_one_axis(self, width: int) -> None:
        """The bottom apex ◆ sits on the C of SANCTUM, and Ki-Adi-Mundi
        (the middle seat) is centred under both — one axis, top to bottom."""
        buf = StringIO()
        console = Console(file=buf, width=width, force_terminal=False, no_color=True)
        b.render_banner(console, animate=False)
        lines = buf.getvalue().splitlines()
        gem_rows = [ln for ln in lines if ln.strip() and set(ln.strip()) <= set("◆◢◣◥◤█")]
        axis = gem_rows[-1].index("◆")
        wordmark = next(ln for ln in lines if "S A N C T U M" in ln)
        assert wordmark.index("C") == axis, "bottom apex must sit on the C of SANCTUM"
        seats = next(ln for ln in lines if "Ki-Adi-Mundi" in ln)
        label_centre = seats.index("Ki-Adi-Mundi") + (len("Ki-Adi-Mundi") - 1) / 2
        # the label is even-width, so the closest a monospace grid allows
        assert abs(label_centre - axis) <= 0.5, "middle seat drifted off the axis"


class TestStaticRender:
    def test_static_banner_contains_wordmark_and_seats(self) -> None:
        # the no-animation path still returns a renderable with the brand + seats
        text = b.static_banner_text()
        assert "S A N C T U M" in text  # the spaced wordmark is the brand
        for seat_label in (
            "Yoda",
            "Windu",
            "Qui-Gon",
            "Ki-Adi-Mundi",
            "Cilghal",
            "Jocasta",
            "Mon Mothma",
        ):
            assert seat_label in text
