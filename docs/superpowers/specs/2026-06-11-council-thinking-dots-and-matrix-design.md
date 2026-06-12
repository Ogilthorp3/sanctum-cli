# Council Thinking-Dots + Matrix UX — Design

**Date:** 2026-06-11
**Status:** approved in conversation; pending written-spec review
**Scope:** three small, related UX features for `sanctum-cli` (plus one user-env setup performed outside the repo)

## Context

The council REPL (`sanctum council`) streams answers token-by-token, but
between submitting a prompt and the first SSE delta there is dead air — the
REPL prints `Yoda: ` and nothing moves. Yoda's seat rides
`council-max-thinking`, a thinking model, so the freeze is long enough to
read as a hang. The `/council` fan-out already has a spinner
("The council deliberates…"); the per-seat chat path has nothing.

Separately, Bert wants Matrix flavour for the CLI: the real terminal-font
treatment (as far as a CLI honestly can — fonts belong to the emulator, not
the program) and a digital-rain easter egg.

## A. Seat thinking indicator (council REPL)

**Behaviour contract:**

- From prompt submit until the first SSE text delta: an animated status line
  `<Label> <verb>…` with rich's `simpleDotsScrolling` spinner, styled in the
  seat's colour. Example: `Yoda ponders…` in green, dots cycling.
- The moment the first delta arrives the status disappears and the existing
  `Label: ` + token-streaming path takes over unchanged — the streaming text
  itself is the animation thereafter.
- An error while waiting clears the status and falls into the existing
  `⚠ seat unavailable` handling. An empty answer prints the nameplate and a
  newline, no crash.
- `/council` fan-out keeps its existing "The council deliberates…" status.

**Implementation:**

- `_stream()` is a lazy generator — the HTTP request fires on first pull.
  In the say-path: open `console.status(...)`, pull `first = next(stream,
  None)` inside it, then print the nameplate and `first`, then continue the
  existing for-loop over the remaining deltas.
- `Seat` (frozen dataclass in `council.py`) gains a `verb: str` field:
  Yoda *ponders*, Windu *deliberates*, Qui-Gon *builds*, Ki-Adi-Mundi
  *computes*, Cilghal *examines*, Jocasta *consults the archives*,
  Mon Mothma *checks the runbook*.
- A pure helper builds the status markup (e.g.
  `thinking_markup(seat) -> str`), so the line is unit-testable.
- `banner._SEATS` is untouched (the banner does not think).

**Tests:**

- Every seat has a non-empty `verb` with no trailing punctuation (the `…` is
  appended by the markup helper).
- `thinking_markup` output contains the label, the verb, the ellipsis, and
  the seat's style.
- Existing REPL parse and transcript tests unaffected.

## B. iTerm2 "Sanctum Matrix" profile (user-env, not repo code)

A CLI cannot set its own font. The honest equivalent: a dedicated iTerm2
profile Bert opts into per window. The default profile is not touched.

- **Font:** Glass TTY VT220 (free, github.com/svofski/glasstty) — the
  canonical readable phosphor-CRT font. True Matrix Code NFI is
  katakana-substitution dingbats and illegible for real work, so it is
  rejected. Install: download the `.ttf` into `~/Library/Fonts/`.
- **Profile:** an iTerm2 **Dynamic Profile** at
  `~/Library/Application Support/iTerm2/DynamicProfiles/sanctum-matrix.json`
  — hot-loaded by iTerm2, no plist surgery. Name "Sanctum Matrix",
  Glass TTY VT220 ~15 pt, phosphor palette (foreground ≈ `#00ff41`,
  background ≈ `#0d0208`, green cursor).
- **Rollback:** delete the JSON and the font file. Nothing else changes.
- **Artifact home:** this is user-env configuration, not CLI code. The
  profile JSON is reproduced in the implementation plan/commit message and
  performed as one-off ops on manoir; nothing ships in the package.
- Terminal.app is left alone.

## C. `sanctum matrix` — digital rain easter egg

New module `sanctum_cli/commands/matrix.py`, registered in `cli.py` as
`@app.command("matrix")`.

**Pure, unit-tested core (no I/O):**

- `ColumnState` dataclass: head row, trail length, per-column speed
  (frames per step), respawn countdown.
- `step_columns(columns, height, rng)` — advance heads, expire columns that
  have fully fallen, respawn with randomized trail/speed.
- `pick_glyph(rng)` — charset: half-width katakana (U+FF66–U+FF9D) plus
  digits, with a rare `◆` (~1/200) — the Sanctum gem hiding in the rain.
- `compose_frame(columns, width, height) -> Text` — bright head
  (pale green, bold), trail fading through greens to dim; exact
  `height`-row × `width`-col grid.

**Shell (thin, untested glue):**

- Gate on `banner.should_animate(console.is_terminal)` — same semantics as
  the launch banner (TTY, `NO_COLOR`, `SANCTUM_NO_ANIM`); refuse politely
  on a pipe instead of spraying frames.
- `Live(screen=True)` on the alternate screen buffer at ~20 fps — the
  terminal restores perfectly on exit. Console size re-read each frame so
  resizes don't crash.
- Runs until Ctrl-C; parting line: `Wake up, Neo… the chamber awaits.`
  (dim), exit 0.

**Tests:**

- Heads advance by exactly one step when their frame counter fires; columns
  respawn after falling past `height + trail`.
- `compose_frame` emits exactly `height` rows and `width` cells per row.
- Glyphs are members of the declared charset.
- Gate: non-TTY refuses (reuses the already-tested `should_animate`).

## Non-goals

- No green re-theme of sanctum-cli output — the amber/holocron brand stays;
  Matrix green lives only inside the rain and the opt-in iTerm2 profile.
- No keypress-to-exit (termios/cbreak) handling in v1 — Ctrl-C only.
- No Terminal.app profile.

## Risks

- **Rain perf:** O(width × height) Text rebuild per frame ≈ 12k cells at
  200×60 — trivial for rich on an M-series Mac at 20 fps.
- **Font download:** one outward fetch from the pinned GitHub repo at
  install time; the `.ttf` lands only in `~/Library/Fonts/`.
- **iTerm2 hot-load:** dynamic profiles appear without restart; if the font
  hasn't been installed first, iTerm2 falls back to Monaco — install order
  in the plan is font, then profile.
