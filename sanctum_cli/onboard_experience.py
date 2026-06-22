"""Apple-grade onboarding experience — pure presentation, no I/O.

The ``onboard`` orchestrator composes the setup from existing primitives (cloud
setup, backup, canary, the recipe gates). This module supplies the *experience*
layer that frames those steps as a single narrated arc:

    Welcome → Your AI → Your Network → Your Data → You're Alive

It is deliberately I/O-free: every helper returns a Rich *renderable* the caller
prints. That keeps it unit-testable (render to a throwaway Console, assert on the
captured text) and keeps the orchestrator the only place that touches the
terminal. Three pieces realize the §2 acceptance criteria:

* :class:`Chapter` — a tiny value object naming a chapter and its one-line *why*.
* :func:`chapter_banner` — the persistent **"Step N of M"** progress indicator,
  with the chapter title and its why-line ("confidence at every step").
* :func:`green_check` — the per-chapter **verify** mark ("Claude connected ✓").
* :func:`recap_card` — the **real ending**: a glance-able summary of the whole
  setup (what was configured, what was skipped) shown before the celebratory
  "Your Sanctum is alive" panel.

No secret, no config, and no network ever flows through here — only labels and
statuses the orchestrator hands in.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True, slots=True)
class Chapter:
    """One named chapter of the onboarding arc: a title and a one-line *why*.

    The *why* is the calm, one-sentence reason the chapter exists ("Sanctum routes
    your prompts to the best model — let's connect yours."). It rides into
    :func:`chapter_banner` so every chapter explains itself before it asks for
    anything — the design spec's "one coherent journey" + "confidence" principles.
    """

    title: str
    why: str


def chapter_banner(n: int, total: int, title: str, why: str) -> Panel:
    """Render the chapter banner: a "Step N of M" indicator + title + why-line.

    The persistent progress counter is the spine of the arc (the design spec's
    "Step N of M" requirement). ``n``/``total`` are shown verbatim so the counter
    tracks the real position rather than a hard-coded pair; ``title`` is the chapter
    name and ``why`` its calm one-liner. Returns a Rich :class:`Panel` (no I/O).
    """
    header = Text.assemble(
        (f"Step {n} of {total}", "bold cyan"),
        ("   ", ""),
        (title, "bold"),
    )
    body = Text(why, style="dim")
    return Panel.fit(
        Text.assemble(header, "\n", body),
        border_style="cyan",
        padding=(0, 1),
    )


def green_check(label: str) -> Text:
    """Render a green check + ``label`` — the per-chapter verify mark.

    "Confidence at every step": each chapter ends with a concrete, green-checked
    confirmation ("Claude connected", "network gear paired"). Pure — returns a Rich
    :class:`Text` the caller prints.
    """
    return Text.assemble(("  ✓ ", "bold green"), (label, "green"))


def recap_card(items: list[tuple[str, str]]) -> Panel:
    """Render the closing recap card: one ``(label, status)`` row per chapter.

    The "real ending": a glance-able summary of the whole setup — what was
    configured AND what was skipped — shown before the celebratory "alive" panel.
    Each tuple is ``(chapter-or-item label, human status)``; a status of
    ``"skipped"`` is shown in a gentle dim style (a skipped piece is a note, not a
    failure — the spec's "total forgiveness"). An empty ``items`` renders a valid
    (degenerate) card rather than raising. Pure — returns a Rich :class:`Panel`.
    """
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left", style="bold")
    table.add_column(justify="left")
    for label, status in items:
        low = status.strip().lower()
        if "fail" in low or "needs attention" in low:
            style = "yellow"  # a failure is NEVER shown in the success colour
        elif "skip" in low:
            style = "dim"  # a skipped piece is a gentle note, not a failure
        else:
            style = "green"
        status_text = Text(status, style=style)
        table.add_row(Text(label), status_text)
    return Panel.fit(
        table,
        title="[bold]your Sanctum at a glance[/]",
        subtitle="[dim]recap[/]",
        border_style="green",
        padding=(1, 2),
    )
