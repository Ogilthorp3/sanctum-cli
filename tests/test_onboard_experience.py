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

import getpass
import warnings
from typing import TYPE_CHECKING
from unittest.mock import patch

import yaml
from rich.console import Console
from typer.testing import CliRunner

from sanctum_cli import onboard_experience as ux
from sanctum_cli.cli import app
from sanctum_cli.commands import onboard
from sanctum_cli.providers.base import HealthSnapshot

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def ux_health_ok() -> HealthSnapshot:
    """A passing provider health snapshot (the verified-key positive)."""
    return HealthSnapshot(ok=True, latency_ms=12, quota_remaining=None, detail=None)


def ux_health_bad() -> HealthSnapshot:
    """A failing provider health snapshot (the rejected-key fail-closed path)."""
    return HealthSnapshot(ok=False, latency_ms=None, quota_remaining=None, detail="401")


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
    """recap_card renders each (label, status) row — configured AND skipped alike.

    The 'Your Data' row status is DERIVED from a canary outcome via the same mapping
    the orchestrator uses (`onboard._canary_recap_status`), not hard-coded — so this
    test can't silently agree with a bug where the recap claims 'verified' regardless
    of the real round-trip (the original hard-coded 'backup + canary ✓' shared exactly
    that wrong assumption). Here we pass a VERIFIED outcome, so the row reads verified.
    """
    from sanctum_cli.commands.onboard import CanaryOutcome, _canary_recap_status

    data_status = _canary_recap_status(CanaryOutcome.VERIFIED)
    out = _render(
        ux.recap_card(
            [
                ("Your AI", "Claude · Gemini"),
                ("Your Network", "skipped"),
                ("Your Data", data_status),
            ]
        )
    )
    assert "Your AI" in out
    assert "Claude" in out
    assert "Your Network" in out
    assert "skipped" in out
    assert "Your Data" in out
    # The derived status is what's rendered — a VERIFIED outcome reads as verified,
    # never a blanket 'verified' regardless of the real outcome.
    assert data_status.split()[0].lower() in out.lower()


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
        patch(
            "sanctum_cli.commands.onboard._run_canary",
            return_value=onboard.CanaryOutcome.VERIFIED,
        ),
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


def test_onboard_failed_canary_does_not_claim_setup_verified(
    full_instance_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Honest finish: a FAILED backup canary must NOT render 'Setup verified'.

    The recap already shows the canary failed; the closing line must agree. The
    Sanctum is still alive (mlx_local floor), but we never claim verification we
    did not earn — the same doctrine the recap/green-checks now follow.
    """
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(full_instance_yaml))
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch(
            "sanctum_cli.commands.onboard._run_canary",
            return_value=onboard.CanaryOutcome.FAILED,
        ),
        patch("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None),
    ):
        result = runner.invoke(app, ["onboard", "--recipe", "family", "--yes"])
    out = " ".join(result.stdout.split())
    assert result.exit_code == 0, out
    assert "Setup verified" not in out, out  # never claimed over a failed canary
    assert "verified the restore" not in out, out  # nor in the celebratory panel prose
    assert "round-tripping" not in out, out  # the panel must agree with the recap
    assert "needs attention" in out, out  # the honest closing
    assert "Your Sanctum is alive" in out, out  # still alive via the mlx_local floor


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


# ── The recap must reflect what was PERSISTED, not "ran interactively" ────
# The §8 acceptance tests above only exercise the --yes path, where everything is
# honestly "skipped" — so a recap heuristic of "ran interactively == configured"
# happens to be correct there and ships the false-"connected" bug unseen. These
# tests drive the INTERACTIVE chapter to completion while configuring NOTHING and
# then assert the recap/green-check reflects that. Crucially, the expectation is
# derived from a DIFFERENT source than the orchestrator's mental model: the actual
# on-disk `cli.providers` in instance.yaml (the contract the recap row claims to
# summarize). A test cannot catch a bug it shares — so we assert "row status ==
# what was written", not "the label rendered".


def _invoke_onboard_interactive(input_text: str) -> tuple[int, str]:
    """Run `onboard --recipe family` (no --yes), feeding stdin to the AI chapter.

    The backup/cloud/canary primitives and the OTHER interactive gates (identity,
    family, firewalla, network-gear) are mocked so they don't consume THIS chapter's
    stdin — leaving the AI chapter as the only live interactive gate. The mocked
    gates return their default (a ``MagicMock`` is truthy), so we override the two
    that feed the recap rows we DON'T assert on (You / Your Network) to return False
    explicitly, keeping the focus on the AI row's honesty.
    """
    with (
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_estimate"),
        patch("sanctum_cli.commands.onboard.backup_cmd.backup_run"),
        patch("sanctum_cli.commands.onboard._dispatch_cloud_setup"),
        patch(
            "sanctum_cli.commands.onboard._run_canary",
            return_value=onboard.CanaryOutcome.VERIFIED,
        ),
        patch("sanctum_cli.commands.onboard._run_identity_setup", return_value=False),
        patch("sanctum_cli.commands.onboard._run_family_setup", return_value=False),
        patch("sanctum_cli.commands.onboard._run_firewalla_pairing", return_value=False),
        patch("sanctum_cli.commands.onboard._run_firewalla_compat", return_value=False),
        patch("sanctum_cli.commands.onboard._run_haus_scan", return_value=False),
        patch("sanctum_cli.commands.onboard._run_network_gear", return_value=False),
        patch("sanctum_cli.commands.onboard._run_ha_green", return_value=False),
        patch("sanctum_cli.commands.onboard._run_network_resilience", return_value=False),
        patch("sanctum_cli.commands.screen_time._fetch_bridge_json", lambda path: None),
        # The masked key prompt routes to getpass, which warns under CliRunner's
        # non-TTY stdin; pyproject turns warnings into errors, so suppress that one.
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore", getpass.GetPassWarning)
        result = runner.invoke(app, ["onboard", "--recipe", "family"], input=input_text)
    return result.exit_code, " ".join(result.stdout.split())


def _recap_ai_status(out: str) -> str:
    """The recap row status for 'Your AI' — read from the rendered recap card.

    The recap card (title "your Sanctum at a glance", subtitle "recap") renders one
    ``label  status`` row per chapter. ``out`` is whitespace-normalized, so the row
    reads ``... Your AI <status> Your Network ...``. We anchor on the recap-card
    TITLE (so we read the recap, not an earlier mention of the chapter), then take
    the first word between the 'Your AI' label and the next row's 'Your Network'
    label — the status the user actually sees. Asserting against THIS row (vs an
    internal flag) is the point: the row is the recap's claim about what was
    persisted.
    """
    card = out[out.index("your Sanctum at a glance") :]
    after_ai = card[card.index("Your AI") + len("Your AI") :]
    status_region = after_ai[: after_ai.index("Your Network")]
    return status_region.strip().split(" ", 1)[0].lower()


def test_interactive_ai_chapter_configuring_nothing_persists_nothing_and_recap_says_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive AI chapter, API path + EMPTY keys → on-disk providers empty AND
    the recap says 'skipped' — never a false 'connected'.

    This is the contract the false-"connected" bug violated: the recap row claims to
    summarize what was PERSISTED. We choose the API-key path (option 2), enter an
    empty Anthropic key, and skip Gemini (empty key) — so NOTHING is written. The
    expectation is derived from the on-disk instance.yaml (a different source than
    the orchestrator): if `cli.providers` is empty/None, the AI recap row MUST read
    'skipped' and the green-check MUST NOT say 'AI connected'.
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))

    # proceed? / run real backup? (defaults) → API path → empty key → skip Gemini.
    code, out = _invoke_onboard_interactive("\n\n2\n\n\n")
    assert code == 0, out

    # 1) Nothing persisted on disk — the AUTHORITATIVE source, written by production.
    data = yaml.safe_load(inst.read_text(encoding="utf-8")) or {}
    providers = data.get("cli", {}).get("providers")
    assert not providers, f"expected no providers persisted, got {providers!r}\n{out}"

    # 2) The recap row for 'Your AI' MUST match that truth — 'skipped', not 'connected'.
    assert _recap_ai_status(out) == "skipped", out

    # 3) The green-check MUST NOT falsely claim a connection.
    assert "AI connected" not in out, out
    assert "AI step ready" in out, out


def test_interactive_ai_chapter_rejected_key_persists_nothing_and_recap_says_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API path, a key the health-probe REJECTS → revoke + persist nothing + recap
    'skipped'.

    Fail-closed: the user enters a non-empty key but the (mocked) health-probe
    rejects it; the key is revoked and no `via=direct` config is written. The recap
    must reflect that on-disk truth as 'skipped', not 'connected'. Mirrors the P4
    auth-probe contract at the experience layer.
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._provider_health",
        lambda kind, cfg: ux_health_bad(),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret", lambda **k: None
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._revoke_device_secret", lambda **k: None
    )

    # API path → a rejected key → skip Gemini.
    code, out = _invoke_onboard_interactive("\n\n2\nsk-bad\n\n")
    assert code == 0, out

    data = yaml.safe_load(inst.read_text(encoding="utf-8")) or {}
    claude = data.get("cli", {}).get("providers", {}).get("claude")
    assert claude is None or claude.get("via") != "direct", data
    assert _recap_ai_status(out) == "skipped", out
    assert "AI connected" not in out, out


def test_interactive_ai_chapter_verified_key_persists_and_recap_says_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest POSITIVE: a verified API key persists `via=direct` AND the recap
    says 'connected'.

    The mirror of the fail-closed tests — proving the signal is not merely 'always
    skipped'. A genuine green health-probe writes `cli.providers.claude.via=direct`,
    so the recap row MUST read 'connected' and the green-check 'AI connected'.
    """
    inst = tmp_path / "instance.yaml"
    inst.write_text("instance:\n  name: X\n  slug: x\n", encoding="utf-8")
    monkeypatch.setenv("SANCTUM_INSTANCE_FILE", str(inst))
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard._provider_health",
        lambda kind, cfg: ux_health_ok(),
    )
    monkeypatch.setattr(
        "sanctum_cli.commands.onboard.store_device_secret", lambda **k: None
    )

    # API path → an accepted key → skip Gemini.
    code, out = _invoke_onboard_interactive("\n\n2\nsk-good\n\n")
    assert code == 0, out

    data = yaml.safe_load(inst.read_text(encoding="utf-8")) or {}
    assert data["cli"]["providers"]["claude"]["via"] == "direct", data
    assert _recap_ai_status(out) == "connected", out
    assert "AI connected" in out, out


# ── First Hello — the DOD1 finish line (fail-soft contract) ──────────────
# The guided path's closing beat. It must reach the operator when installed,
# and must NEVER turn a completed onboarding into a failure when it can't.

def test_first_hello_absent_script_is_a_silent_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sanctum-first-hello.py installed → silent skip, no output, no raise."""
    monkeypatch.setenv("HOME", str(tmp_path))  # a home with no ~/.sanctum/bin script
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)  # env-independent home
    with patch("sanctum_cli.commands.onboard.console") as mock_console:
        onboard._run_first_hello("Bert")  # must not raise
    # Nothing printed — a haus with no First Hello installed just stays quiet.
    assert not mock_console.print.called


def test_first_hello_runs_script_and_passes_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Script present → announces + invokes it with SANCTUM_USER_NAME set."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Pin home directly, not just via $HOME: _run_first_hello resolves the script
    # through Path.home(), and relying on the env alone made this test flake once
    # in a full-suite run when home resolved to a script-less dir. Pinning makes
    # it hermetic regardless of ambient process state.
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    script = tmp_path / ".sanctum" / "bin" / "sanctum-first-hello.py"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env python3\n")
    with patch("subprocess.run") as mock_run, patch(
        "sanctum_cli.commands.onboard.console"
    ) as mock_console:
        onboard._run_first_hello("Bert")
    assert mock_run.called, "First Hello must invoke the installed script"
    env = mock_run.call_args.kwargs.get("env", {})
    assert env.get("SANCTUM_USER_NAME") == "Bert"
    assert mock_console.print.called  # the "haus wants to say hello" beat


def test_first_hello_never_breaks_onboarding_when_script_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finicky TTS / crashing script is suppressed — onboarding stays complete."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)  # env-independent home
    script = tmp_path / ".sanctum" / "bin" / "sanctum-first-hello.py"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env python3\n")
    with patch(
        "subprocess.run",
        side_effect=OSError("no audio device"),
    ), patch("sanctum_cli.commands.onboard.console"):
        onboard._run_first_hello("Bert")  # must swallow the error, not raise
