"""sanctum onboard — Apple-grade experience layer (Task 2).

Two halves:

  * Pure presentation unit tests over ``sanctum_cli.onboard_experience`` — the
    ``Chapter`` model, ``chapter_banner(n, total, title, why)`` ("Step N of M"
    indicator + title + why-line), ``green_check(label)``, and ``recap_card(items)``
    (lists configured + skipped). No I/O, so they render to a throwaway Console and
    assert on the captured text.

  * Acceptance tests over the wired ``onboard`` orchestrator: every chapter emits a
    "Step N of M" banner, each chapter ends with a green check, a recap card renders
    before the existing "alive" panel, and ``--yes`` still completes non-interactively.

These pin the §2 acceptance criteria from the design spec (progress counter present,
per-chapter verify line, forgiving skip notes, the final recap).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from rich.console import Console
from typer.testing import CliRunner

from sanctum_cli import onboard_experience as ux
from sanctum_cli.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _render(renderable: object) -> str:
    """Render a Rich renderable to plain text (no markup, no color) for assertions."""
    console = Console(width=100, no_color=True, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# ── Pure presentation: Chapter / chapter_banner / green_check / recap_card ──


def test_chapter_model_carries_name_and_why() -> None:
    """A Chapter is a tiny value object: a title and a one-line why."""
    ch = ux.Chapter(title="Your AI", why="connect your models")
    assert ch.title == "Your AI"
    assert ch.why == "connect your models"


def test_chapter_banner_renders_step_n_of_m_with_title_and_why() -> None:
    """chapter_banner(2, 5, ...) shows a 'Step 2 of 5' counter + the title + the why."""
    out = _render(ux.chapter_banner(2, 5, "Your AI", "let's connect your models"))
    assert "Step 2 of 5" in out
    assert "Your AI" in out
    assert "let's connect your models" in out


def test_chapter_banner_counter_tracks_position() -> None:
    """The counter reflects the actual (n, total) — not a hard-coded pair."""
    assert "Step 1 of 5" in _render(ux.chapter_banner(1, 5, "Welcome", "w"))
    assert "Step 4 of 5" in _render(ux.chapter_banner(4, 5, "Your Data", "d"))
    assert "Step 3 of 7" in _render(ux.chapter_banner(3, 7, "Mid", "m"))


def test_green_check_renders_a_check_and_the_label() -> None:
    """green_check('Claude') renders a check glyph next to the label."""
    out = _render(ux.green_check("Claude connected"))
    assert "Claude connected" in out
    assert "✓" in out or "✓" in out


def test_recap_card_lists_configured_and_skipped_items() -> None:
    """recap_card renders each (label, status) row — configured AND skipped alike."""
    out = _render(
        ux.recap_card(
            [
                ("Your AI", "Claude · Gemini"),
                ("Your Network", "skipped"),
                ("Your Data", "backup + canary ✓"),
            ]
        )
    )
    assert "Your AI" in out
    assert "Claude" in out
    assert "Your Network" in out
    assert "skipped" in out
    assert "Your Data" in out


def test_recap_card_with_empty_items_still_renders() -> None:
    """An empty recap is a valid (degenerate) card — never raises."""
    out = _render(ux.recap_card([]))
    assert isinstance(out, str)


# ── Acceptance: the wired onboard orchestrator ───────────────────────────


def _invoke_onboard_yes(recipe: str = "family") -> tuple[int, str]:
    """Run `onboard --recipe <recipe> --yes` with the backup primitives mocked.

    Returns (exit_code, whitespace-normalized stdout). The interactive recipe
    gates all skip under --yes, so no stdin is consumed.
    """
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch("sanctum_cli.commands.onboard._run_canary"),
        patch("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", recipe, "--yes"])
    return result.exit_code, " ".join(result.stdout.split())


def test_onboard_emits_step_counter_for_every_chapter(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrated arc shows a persistent 'Step N of M' indicator across chapters."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    code, out = _invoke_onboard_yes()
    assert code == 0, out
    # Five named chapters → Step 1..5 of 5, each present once.
    for n in range(1, 6):
        assert f"Step {n} of 5" in out, f"missing Step {n} of 5\n{out}"


def test_onboard_names_the_apple_arc_chapters(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named chapters (Welcome / Your Data / You / Your AI / Your Network) appear.

    ORDERING DECISION: the spec arc reads Welcome → Your AI → Your Network → Your
    Data, but the gates-before-data reorder broke 18 existing interactive onboard
    tests (their stdin assumes the proceed/backup confirms come first), so per the
    plan we KEPT the existing execution order and frame the arc over it. These are
    the chapter names in the order they actually run.
    """
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    code, out = _invoke_onboard_yes()
    assert code == 0, out
    for title in ("Welcome", "Your Data", "Your AI", "Your Network"):
        assert title in out, f"missing chapter {title!r}\n{out}"
    # The real ending is the existing celebratory panel.
    assert "Your Sanctum is alive" in out


def test_onboard_emits_a_green_check_per_chapter(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each chapter ends with a verify green-check — confidence at every step."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    code, out = _invoke_onboard_yes()
    assert code == 0, out
    # At least one green check per named chapter (5). Count the check glyph.
    assert out.count("✓") >= 5, f"expected >=5 green checks, got {out.count('✓')}\n{out}"


def test_onboard_renders_recap_card_before_alive_panel(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recap card summarizing the journey renders before the 'alive' ending."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    code, out = _invoke_onboard_yes()
    assert code == 0, out
    assert "recap" in out.lower(), out
    # The recap precedes the celebratory ending.
    assert out.lower().index("recap") < out.index("Your Sanctum is alive"), out


def test_onboard_recap_lists_configured_and_skipped(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --yes the interactive chapters skip — the recap names them as skipped."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    code, out = _invoke_onboard_yes()
    assert code == 0, out
    # The recap card carries the chapter labels and a 'skipped' status for the
    # interactive chapters that --yes bypassed.
    assert "Your AI" in out
    assert "Your Network" in out
    assert "skipped" in out


def test_onboard_yes_completes_non_interactively(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes never hangs on stdin and reaches the celebratory ending."""
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    code, out = _invoke_onboard_yes()
    assert code == 0, out
    assert "onboarding complete" in out


# ── Characterization: the ordering decision (kept existing order) ────────


def test_arc_keeps_existing_data_then_tools_order(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrated arc reads Your Data → Your AI → Your Network (existing order kept).

    ORDERING DECISION recorded: the spec wanted tools-before-data, but moving the
    recipe gates ahead of the cloud/backup block broke 18 existing interactive
    onboard tests whose stdin sequences assume the proceed/backup confirms are
    consumed before the gate prompts. Per the plan's explicit fallback we did NOT
    force the reorder; we kept the existing execution order and frame the Apple arc
    + recap over it. This pins that chosen order so the decision is testable.
    """
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    code, out = _invoke_onboard_yes()
    assert code == 0, out
    assert out.index("Your Data") < out.index("Your AI") < out.index("Your Network"), out
