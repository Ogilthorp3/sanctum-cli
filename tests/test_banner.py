"""Sanctum council banner — the kyber-diamond launch animation.

Tests cover the PURE parts: the diamond art geometry, the vertical reveal
frame sequence, the gradient stop math, and the policy that decides whether
to animate. The Live animation itself is exercised only through the policy
gate (a non-TTY / NO_COLOR / SANCTUM_NO_ANIM env must never animate).
"""

from __future__ import annotations

import pytest

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


class TestStaticRender:
    def test_static_banner_contains_wordmark_and_seats(self) -> None:
        # the no-animation path still returns a renderable with the brand + seats
        text = b.static_banner_text()
        assert "S A N C T U M" in text  # the spaced wordmark is the brand
        for seat_label in ("Yoda", "Windu", "Qui-Gon", "Ki-Adi-Mundi", "Cilghal", "Jocasta"):
            assert seat_label in text
