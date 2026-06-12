# Council Thinking-Dots + Matrix UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Animated per-seat thinking indicator in the council REPL, a `sanctum matrix` digital-rain easter egg, and an opt-in iTerm2 "Sanctum Matrix" phosphor profile.

**Architecture:** Feature A threads a `console.status()` spinner into the dead-air between prompt submit and first SSE delta in `council.py` (the `_stream` generator is lazy, so pulling the first delta inside the status is the whole trick). Feature C is a new `commands/matrix.py` with a pure, unit-tested core (column state machine, glyph picker, frame composer) under a thin `Live(screen=True)` shell, registered in `cli.py` like every other command. Feature B is one-off user-env ops (font + iTerm2 dynamic profile), no repo code.

**Tech Stack:** Python 3.12 (dev venv at `.venv/`), rich (Status/Live/Text/Style), typer, pytest, ruff, mypy --strict. Repo: `~/Projects/sanctum-cli`. Run everything from the repo root with `.venv/bin/...`.

**Spec:** `docs/superpowers/specs/2026-06-11-council-thinking-dots-and-matrix-design.md`

**House rules that bind every task:** repo gates are `.venv/bin/ruff check sanctum_cli tests`, `.venv/bin/ruff format <touched files>`, `.venv/bin/mypy sanctum_cli`, `.venv/bin/python -m pytest -q` — all must be green before each commit. Comment style is full sentences explaining *why*, module docstrings explain the design (see `banner.py` as the exemplar).

---

### Task 1: Seat verbs + `thinking_markup` (Feature A, pure part)

**Files:**
- Modify: `sanctum_cli/commands/council.py` (Seat dataclass ~line 48, SEATS dict ~lines 58–151)
- Test: `tests/test_council.py` (add a new class after `TestSeats`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_council.py` (it imports the module as `from sanctum_cli.commands import council as cc` — line 18):

```python
class TestThinkingIndicator:
    def test_every_seat_waits_in_character(self) -> None:
        # every seat has a verb, and the verb composes cleanly with the
        # ellipsis the markup helper appends
        for seat in cc.SEATS.values():
            assert seat.verb, f"{seat.label} has no thinking verb"
            assert seat.verb[-1] not in ".…!?", f"{seat.label} verb carries punctuation"

    def test_thinking_markup_carries_label_verb_and_colour(self) -> None:
        seat = cc.SEATS["yoda"]
        line = cc.thinking_markup(seat)
        assert "Yoda ponders…" in line
        assert f"[{seat.style}]" in line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_council.py::TestThinkingIndicator -v`
Expected: 2 FAILED — `TypeError`/`AttributeError` (no `verb` field, no `thinking_markup`).

- [ ] **Step 3: Implement — `verb` field, seven verbs, markup helper**

In `council.py`, extend the frozen dataclass:

```python
@dataclass(frozen=True)
class Seat:
    """One council chair: a persona riding a proxyd model."""

    label: str
    model: str
    persona: str
    style: str  # rich color for the nameplate
    verb: str  # what the seat does while it thinks ("ponders", …)
```

Add to each `Seat(...)` in `SEATS` (after its `style=` line):

| key | verb |
|---|---|
| yoda | `verb="ponders",` |
| windu | `verb="deliberates",` |
| quigon | `verb="builds",` |
| mundi | `verb="computes",` |
| cilghal | `verb="examines",` |
| jocasta | `verb="consults the archives",` |
| mothma | `verb="checks the runbook",` |

Then add the pure helper directly below the `SEATS` dict:

```python
def thinking_markup(seat: Seat) -> str:
    """The status line shown while a seat thinks — in character, in colour."""
    return f"[{seat.style}]{seat.label} {seat.verb}…[/]"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_council.py -v`
Expected: ALL PASS (the new 2 plus every pre-existing council test).

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/council.py tests/test_council.py
.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q
git add sanctum_cli/commands/council.py tests/test_council.py
git commit -m "feat(council): per-seat thinking verbs + status markup helper"
```

Note: `council.py` carries uncommitted Mon-Mothma-red work from earlier in the session — committing the whole file here is fine and intended; mention it in the commit body if the diff still contains it:
`style red for Mon Mothma rides along (separately approved).`

---

### Task 2: REPL thinking-status handoff (Feature A, glue)

**Files:**
- Modify: `sanctum_cli/commands/council.py` — the say/switch_say tail of `_repl()` (~lines 428–441)

- [ ] **Step 1: Replace the streaming tail of `_repl()`**

Current code:

```python
        seat = SEATS[active]
        transcript.add("user", action.arg)
        console.print(f"[{seat.style}]{seat.label}:[/] ", end="")
        chunks: list[str] = []
        try:
            for delta in _stream(seat, transcript.messages(), system=seat.persona):
                chunks.append(delta)
                console.print(delta, end="", soft_wrap=True)
            console.print()
        except Exception as e:
            console.print(f"\n[red]⚠ {e}[/]")
            transcript.add("assistant", "(seat unavailable)")
            continue
        transcript.add("assistant", "".join(chunks))
```

New code:

```python
        seat = SEATS[active]
        transcript.add("user", action.arg)
        chunks: list[str] = []
        try:
            # _stream is lazy — the request fires on the first pull, so the
            # status animates exactly across the model's thinking dead-air
            # and vanishes the moment the first word lands.
            stream = _stream(seat, transcript.messages(), system=seat.persona)
            with console.status(
                thinking_markup(seat), spinner="simpleDotsScrolling", spinner_style=seat.style
            ):
                first = next(stream, None)
            console.print(f"[{seat.style}]{seat.label}:[/] ", end="")
            if first is not None:
                chunks.append(first)
                console.print(first, end="", soft_wrap=True)
                for delta in stream:
                    chunks.append(delta)
                    console.print(delta, end="", soft_wrap=True)
            console.print()
        except Exception as e:
            console.print(f"\n[red]⚠ {e}[/]")
            transcript.add("assistant", "(seat unavailable)")
            continue
        transcript.add("assistant", "".join(chunks))
```

Behaviour notes the engineer must preserve:
- An exception inside `next(stream, None)` propagates out of the `with` (status cleared by the context manager) into the existing `except` — the `⚠ seat unavailable` path is unchanged.
- An empty stream (`first is None`) prints the bare nameplate + newline — no crash, transcript records `""`.
- `simpleDotsScrolling` is a stock rich spinner (`python -m rich.spinner` lists it); it renders literally as a marching `...`.

- [ ] **Step 2: Run the full suite (no new unit test — this is I/O glue; the pure part was Task 1)**

Run: `.venv/bin/python -m pytest -q`
Expected: ALL PASS.

- [ ] **Step 3: Live smoke (only if proxyd is up on :4040)**

Run `sanctum council` in a real terminal, ask Yoda anything, observe: `Yoda ponders…` with marching dots → first word replaces it → streaming as before. Also `/quit` cleanly. If proxyd is down, skip — the refusal path is the existing ⚠ handling.

- [ ] **Step 4: Gates + commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/council.py
.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q
git add sanctum_cli/commands/council.py
git commit -m "feat(council): animated thinking status until the first streamed token"
```

---

### Task 3: `matrix.py` pure core — column mechanics (Feature C)

**Files:**
- Create: `sanctum_cli/commands/matrix.py`
- Create: `tests/test_matrix.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_matrix.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_matrix.py -v`
Expected: collection error — `ModuleNotFoundError: sanctum_cli.commands.matrix`.

- [ ] **Step 3: Create `sanctum_cli/commands/matrix.py` with the column core**

```python
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
from dataclasses import dataclass

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


@dataclass(frozen=True)
class ColumnState:
    """One column of rain: a bright head dragging a fading trail."""

    head: int  # row of the head glyph; negative while sliding in from above
    trail: int  # glyphs in the trail behind the head
    period: int  # frames per one-row fall (1 = falls every frame)
    phase: int = 0  # frame counter modulo period


def spawn_column(height: int, rng: random.Random) -> ColumnState:
    """A fresh column: head above the screen so it slides in, not pops in."""
    return ColumnState(
        head=-rng.randrange(height),
        trail=rng.randrange(3, max(4, height)),
        period=rng.choice((1, 1, 2, 3)),
    )


def step_column(col: ColumnState, height: int, rng: random.Random) -> ColumnState:
    """Advance one frame; respawn once the whole trail has drained off-screen."""
    phase = (col.phase + 1) % col.period
    if phase != 0:
        return ColumnState(col.head, col.trail, col.period, phase)
    if col.head - col.trail > height:
        return spawn_column(height, rng)
    return ColumnState(col.head + 1, col.trail, col.period, phase)
```

(`typer`, `time`, `Live`, `Color`, `Style`, `Text`, `should_animate`, the palette constants and `console` are unused until Tasks 4–5 — if ruff flags unused imports at this commit, trim to what Task 3 uses and re-add in the task that needs them. Keep `from __future__ import annotations` regardless.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_matrix.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/matrix.py tests/test_matrix.py
.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q
git add sanctum_cli/commands/matrix.py tests/test_matrix.py
git commit -m "feat(matrix): digital-rain column state machine"
```

If ruff fires `S311` (pseudo-random) anywhere: this is decorative randomness, not crypto — suppress at the line with `# noqa: S311` and a trailing comment saying so.

---

### Task 4: `matrix.py` glyphs + frame composer (Feature C)

**Files:**
- Modify: `sanctum_cli/commands/matrix.py`
- Modify: `tests/test_matrix.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_matrix.py`:

```python
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

        assert all(cell_len(g) == 1 for g in m.GLYPHS + [m.GEM])


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_matrix.py -v`
Expected: Task 3's 4 still PASS; the new 5 FAIL with `AttributeError` (`pick_glyph`, `GLYPHS`, `GEM`, `compose_frame`, `trail_rgb` undefined).

- [ ] **Step 3: Implement glyphs + composer**

Add to `matrix.py` (below the palette constants):

```python
# Charset: half-width katakana (single-cell — full-width would shear the
# grid) plus digits, with a rare Sanctum gem hiding in the rain.
GLYPHS = [chr(cp) for cp in range(0xFF66, 0xFF9E)] + list("0123456789")
GEM = "◆"
GEM_RARITY = 200  # ~one gem per 200 glyphs


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


def compose_frame(
    columns: list[ColumnState], width: int, height: int, rng: random.Random
) -> Text:
    """One full screen of rain: exactly `height` rows by `width` cells."""
    frame = Text()
    for row in range(height):
        for x in range(width):
            col = columns[x]
            distance = col.head - row
            if distance == 0:
                frame.append(pick_glyph(rng), Style(color=Color.from_rgb(*_HEAD), bold=True))
            elif 0 < distance <= col.trail:
                frame.append(pick_glyph(rng), Style(color=Color.from_rgb(*trail_rgb(distance, col.trail))))
            else:
                frame.append(" ")
        if row < height - 1:
            frame.append("\n")
    return frame
```

(The `# type: ignore[return-value]` on the tuple comprehension is the established pattern — `banner.gradient_rgb` does exactly this.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_matrix.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/matrix.py tests/test_matrix.py
.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q
git add sanctum_cli/commands/matrix.py tests/test_matrix.py
git commit -m "feat(matrix): glyph charset + phosphor frame composer"
```

---

### Task 5: `sanctum matrix` shell + CLI registration (Feature C)

**Files:**
- Modify: `sanctum_cli/commands/matrix.py` (append the command shell)
- Modify: `sanctum_cli/cli.py` (import ~line 33 alphabetical block, command after `devices_top` ~line 256)
- Modify: `tests/test_matrix.py` (refusal smoke via CliRunner)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_matrix.py`:

```python
class TestCommandGate:
    def test_matrix_refuses_without_a_tty(self) -> None:
        from typer.testing import CliRunner

        from sanctum_cli.cli import app

        result = CliRunner().invoke(app, ["matrix"])
        assert result.exit_code == 1
        assert "terminal" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_matrix.py::TestCommandGate -v`
Expected: FAIL — exit code 2 (typer: no such command "matrix").

- [ ] **Step 3: Implement the shell in `matrix.py`**

Append:

```python
def matrix_command() -> None:
    """``sanctum matrix`` — there is no spoon, only launchd."""
    if not should_animate(console.is_terminal):
        console.print("[red]The Matrix needs a real terminal[/] — not a pipe.")
        raise typer.Exit(1)
    rng = random.Random()
    width, height = console.size.width, console.size.height
    columns = [spawn_column(height, rng) for _ in range(width)]
    try:
        with Live(console=console, screen=True, refresh_per_second=20, transient=True) as live:
            while True:
                # re-read the size every frame so a resize reshapes the rain
                # instead of crashing it
                width, height = console.size.width, console.size.height
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
```

- [ ] **Step 4: Register in `cli.py`**

Add to the alphabetical import block (between `logs` and `onboard`):

```python
from sanctum_cli.commands import matrix as matrix_cmd
```

Add after `devices_top` (~line 256), matching the house wrapper style:

```python
@app.command("matrix", help="Follow the white rabbit — digital rain until Ctrl-C.")
def matrix_top() -> None:
    matrix_cmd.matrix_command()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_matrix.py -v`
Expected: 10 PASS.

- [ ] **Step 6: Live smoke in a real terminal**

Run `sanctum matrix` in iTerm2 (NOT through a pipe): rain falls, gems occasionally glint, Ctrl-C restores the screen and prints the parting line. Then `sanctum matrix | cat` → refusal + exit 1.

- [ ] **Step 7: Gates + commit**

```bash
.venv/bin/ruff format sanctum_cli/commands/matrix.py sanctum_cli/cli.py tests/test_matrix.py
.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q
git add sanctum_cli/commands/matrix.py sanctum_cli/cli.py tests/test_matrix.py
git commit -m "feat(cli): sanctum matrix — digital rain easter egg"
```

---

### Task 6: Banner-axis work rides home (pre-existing working-tree changes)

**Files:**
- Already modified earlier this session (approved interactively, all gates green): `sanctum_cli/commands/banner.py`, `tests/test_banner.py`

- [ ] **Step 1: Confirm the working tree holds only expected changes**

Run: `git status --short`
Expected: only `banner.py` / `test_banner.py` modified (council.py was committed in Tasks 1–2).

- [ ] **Step 2: Re-run full gates**

Run: `.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q`
Expected: all green (425+ tests).

- [ ] **Step 3: Commit**

```bash
git add sanctum_cli/commands/banner.py tests/test_banner.py
git commit -m "fix(banner): one centre axis — diamond, wordmark C, middle seat

Kill the justify/Align double-centering (the leaning-diamond bug),
make the gem all-odd-width so the ◆ apexes sit dead centre, hand-pad
the seat line so Ki-Adi-Mundi rides the same axis. Contract tests at
the Rich boundary assert the axis at even+odd console widths."
```

---

### Task 7: iTerm2 "Sanctum Matrix" profile (Feature B — user-env ops, no repo code)

**Files:**
- Create (user-env): `~/Library/Fonts/Glass_TTY_VT220.ttf`
- Create (user-env): `~/Library/Application Support/iTerm2/DynamicProfiles/sanctum-matrix.json`

- [ ] **Step 1: Locate + download the font (install BEFORE the profile, else iTerm falls back to Monaco)**

```bash
curl -s https://api.github.com/repos/svofski/glasstty/contents/ | grep -iE '"name".*\.ttf'
# note the exact filename, then:
curl -sL -o ~/Library/Fonts/Glass_TTY_VT220.ttf \
  "https://raw.githubusercontent.com/svofski/glasstty/master/<EXACT-FILENAME-FROM-ABOVE>"
file ~/Library/Fonts/Glass_TTY_VT220.ttf
```

Expected: `file` reports a TrueType font. If the repo layout differs, browse the API listing for the `.ttf` path — do NOT fetch from random font-aggregator sites.

- [ ] **Step 2: Resolve the font's real PostScript name**

```bash
system_profiler SPFontsDataType 2>/dev/null | grep -B2 -A8 -i "glass"
```

Expected: a block with `PostScript Name:` (likely `Glass_TTY_VT220` or `GlassTTYVT220`). Use THAT exact string in the profile JSON below.

- [ ] **Step 3: Write the dynamic profile**

Create `~/Library/Application Support/iTerm2/DynamicProfiles/sanctum-matrix.json` (substitute the PostScript name from Step 2):

```json
{
  "Profiles": [
    {
      "Name": "Sanctum Matrix",
      "Guid": "sanctum-matrix-2026-06-11",
      "Normal Font": "<POSTSCRIPT-NAME> 15",
      "Use Non-ASCII Font": false,
      "Foreground Color": {"Red Component": 0.0, "Green Component": 1.0, "Blue Component": 0.255},
      "Background Color": {"Red Component": 0.051, "Green Component": 0.008, "Blue Component": 0.031},
      "Cursor Color": {"Red Component": 0.0, "Green Component": 1.0, "Blue Component": 0.255},
      "Cursor Text Color": {"Red Component": 0.0, "Green Component": 0.0, "Blue Component": 0.0},
      "Bold Color": {"Red Component": 0.8, "Green Component": 1.0, "Blue Component": 0.8}
    }
  ]
}
```

Validate: `plutil -lint "~/Library/Application Support/iTerm2/DynamicProfiles/sanctum-matrix.json"` → OK. iTerm2 hot-loads dynamic profiles within seconds — no restart.

- [ ] **Step 4: Verify with the user**

Bert opens iTerm2 → Profiles → "Sanctum Matrix" → new window → `sanctum matrix`. If glyphs render in Monaco, the PostScript name is wrong — re-run Step 2 and fix the JSON.

**Rollback:** `rm ~/Library/Fonts/Glass_TTY_VT220.ttf "~/Library/Application Support/iTerm2/DynamicProfiles/sanctum-matrix.json"` — nothing else was touched.

---

### Task 8: Final gates + board release

- [ ] **Step 1: Full suite, lint, types — one last sweep**

Run: `.venv/bin/ruff check sanctum_cli tests && .venv/bin/mypy sanctum_cli && .venv/bin/python -m pytest -q`
Expected: all green, ~435 tests.

- [ ] **Step 2: Release the vault board claim**

```bash
~/Projects/openclaw-skills/memory-vault/scripts/vault.sh board set \
  --from claude-2a65c127 --note "council thinking-dots + matrix SHIPPED | released"
```

- [ ] **Step 3: Report to Bert** — commits list, what to try (`sanctum council` for the dots, `sanctum matrix` for the rain, the new iTerm2 profile), and whether to push.
