# Yoda Hybrid Transport — "Gemini hands, Opus voice" — Design

**Date:** 2026-06-13
**Status:** approved in conversation; pending written-spec review
**Provenance:** the transport solution for "Yoda fully operational" (CLI Yoda can
use tools like Signal-Yoda). Supersedes the deferred "fix the :3456 bridge" plan:
the Claude Max bridge wraps `claude --print` and **strips the `tools` array**, and
the Max subscription has no raw Messages-API access, so Opus cannot drive the
client-executes-tools protocol. Bert's call (2026-06-13): keep Opus as the brain
on flat-rate Max, and hand the tool-calling to Gemini 3.1 Pro on the flat-rate
Google AI Studio Ultra sub — both subscriptions, zero marginal token cost.

## Context

CLI Yoda's tool loop (`council.py` `_tool_turn`, shipped 2026-06-12) speaks the
Anthropic tool protocol and is hardened/bounded. But Yoda's seat
(`council-max-thinking`) and every Opus path in proxyd route to the `:3456` Max
bridge, which strips tools — so armed turns always degrade to chat. Meanwhile
proxyd already exposes **`gemini-31-pro`** (`gemini-3.1-pro-preview`, key
`gemini-api-key` in Keychain) and its tool-calling through proxyd's `translate.rs`
was verified in phase-0 (gemini-25-flash). So the parts exist; this spec wires
them into a two-stage armed turn.

## Approach — two-stage armed turn

An armed seat that has a `tool_model` runs each turn in two phases:

1. **Gather (Gemini 3.1 Pro).** The bounded tool loop runs on `seat.tool_model`.
   Gemini decides which instruments to call; the council's own executor runs them
   (audit, redaction, cap, breaker — all unchanged; Gemini only emits `tool_use`
   blocks, the CLI executes them). The phase returns the **findings**: the ordered
   list of `(tool_name, params, result_text)` it gathered (possibly empty), plus
   Gemini's own final text as a fallback.
2. **Voice (Opus via Max).** `seat.model` produces the final answer in Yoda's
   canon voice. Opus never receives the tool protocol — the findings are
   **flattened to plain text** ("Instruments consulted: sanctum_status → …;
   logs_tail(r2d2) → …") and embedded in a user message alongside the original
   question. Opus streams the answer (existing `_stream` path), so the bridge's
   plain-text-only limitation is a non-issue.

When Gemini calls no tools (a pure-chat turn), phase 1 is a fast no-op with empty
findings and phase 2 is just Opus answering directly — i.e. **"Opus when you
don't need tools."** When tools are needed, **"Gemini when you need tool
calling,"** and Opus always does the talking. Gemini never speaks to Bert, so the
canon voice is always Opus — the persona-on-a-foreign-model concern disappears.

## Components

### A. `Seat.tool_model: str | None = None` (`council.py`)
New optional field. Semantics:
- `tools` set **and** `tool_model` set → two-stage gather/voice (this spec).
- `tools` set, `tool_model` **unset** → today's single-model `_tool_turn` (back-compat).
- `tools` empty → today's streaming chat (unchanged).

Yoda and Mon Mothma (the armed seats) gain `tool_model="gemini-31-pro"`. Their
`model` (the voice) is unchanged (`council-max-thinking` / `council-brain`).

### B. Gather phase — reuse the hardened loop on a model override
The buffered tool loop is parameterized by the model used for `_post_with_tools`
(today it is `seat.model`; the gather phase passes `seat.tool_model`). The loop
keeps every existing bound — cap, two-error breaker, `MAX_TOOL_LOOP_ITERATIONS`,
malformed-block drop/synthesis, empty-results early return — and additionally
**records each executed tool's `(name, params, redacted result)`** so the voice
phase has the findings. Output: a `ToolFindings` value (the exchanges + Gemini's
final text). The malformed-block hardening matters here: Gemini tool blocks arrive
via proxyd translation and can be ragged.

### C. Voice phase — `_voice_answer(seat, question, findings) -> streamed str`
Builds a user message = original question + a flattened, human-readable findings
block (or, if findings are empty, just the question). System prompt =
`_persona(seat, armed=False)` — the canon voice with the **unarmed** clause,
because the voice model has no tools of its own this phase (bare `_persona(seat)`
would hand an armed seat the instruments clause, which is wrong here). Streams via
the existing `_stream` path against `seat.model`. The streamed text is the turn's
answer and what lands in the transcript.

### D. `_say_turn` wiring
For an armed seat **with** a `tool_model`: gather → voice. The "consults the
instruments" spinner covers phase 1; phase 2 streams Yoda's reply live.
ToolsRejected / errors handled by the fallbacks below.

### E. proxyd (config only, no Rust change)
`gemini-31-pro` already resolves (provider `gemini`, key wired). No proxyd code
change is required — the gather phase simply targets that existing model id. (If a
dedicated seat alias reads cleaner, add a `council-hands` → `gemini-31-pro` entry;
optional, decided in the plan.)

## Data flow

```
user ─▶ _say_turn(armed seat, tool_model set)
          │
          ├─ phase 1 GATHER  ── _post_with_tools(model=tool_model=gemini-31-pro)
          │     proxyd :4040 ─ translate.rs ─▶ gemini API (tools work)
          │     tool_use ─▶ council run_tool (audit/redact/cap/breaker) ─▶ findings
          │
          └─ phase 2 VOICE  ── _stream(model=seat.model=council-max-thinking)
                proxyd :4040 ─▶ :3456 Max bridge ─▶ claude --print (plain text, no tools)
                Opus streams the canon-voice answer from question + flattened findings
```

## Error handling / degrade

- **Gemini rejects tools / 4xx (ToolsRejected on first POST):** fall back to
  today's behavior — Opus chat-only on `seat.model` with the unarmed persona
  (`_persona(seat, armed=False)`), with the dim "(declines tools — chat only)"
  note. No regression vs. today.
- **Gemini mid-loop failure / breaker / cap:** the gather phase returns whatever
  findings it has (partial is fine); the voice phase synthesizes from them.
- **Opus voice phase fails (bridge down, HTTP error):** fall back to returning
  Gemini's own gathered final text (the `ToolFindings` fallback string), so the
  user still gets the data even if the voice model is down. Markup-safe printing
  throughout (the `Text(...)` / `escape()` discipline already in `_say_turn`).
- **Ctrl-C** in either phase aborts the turn cleanly → "(turn aborted)" (today's
  contract preserved).

## Non-goals

- **No tool-need gate in v1.** Gemini runs on every armed turn (a no-op when no
  tool is needed). The upfront "does this need live state?" classifier that would
  skip phase 1 on obvious chat is a documented latency optimization for a later
  pass, not this spec.
- **No fan-out tooling.** `/council` stays chat-only and on each seat's `model`
  (unchanged).
- **No new mutate tools** (phase-1 read-only registry unchanged).
- **No proxyd Rust changes** (config-only at most).
- **No bridge fix.** This spec retires that approach for CLI Yoda.

## Testing

- **Gather records findings:** a faked Gemini transport drives a `tool_use` →
  result sequence; assert the returned findings carry each executed tool's
  `(name, params, result)` and that the existing bounds (cap, breaker,
  malformed-drop, empty-results return, hard backstop) still hold.
- **Voice gets text, never protocol:** a fake `_stream` captures the system +
  messages Opus receives; assert NO `tool_use`/`tool_result` blocks reach it and
  that the flattened findings text is present. The persona is the unarmed clause.
- **No-tool turn = Opus-only:** Gemini fake returns immediate `end_turn` with no
  tools → findings empty → voice fake receives just the question → its streamed
  text is the answer.
- **Flattening is faithful and bounded:** redacted results only (reuse the
  redaction path); long results truncated under a byte budget so the voice prompt
  can't blow up.
- **Degrade paths:** Gemini ToolsRejected → unarmed Opus chat; Opus-voice
  exception → Gemini fallback text. Both asserted, markup-safe.
- **Back-compat:** a seat with `tools` but no `tool_model` still uses the existing
  single-model `_tool_turn` (the 2026-06-12 suite stays green).
- **Live smoke (manual, gated on billing confirm):** one real armed turn —
  "check the agents and tell me" — Gemini calls `agent_list`, Opus voices the
  result in canon English. Quoted in the implementation report.

## Risks

- **Two round-trips per armed turn** (Gemini gather + Opus voice) — latency on
  chat turns where Gemini no-ops. Accepted for v1; the gate is the fix if it
  annoys.
- **`gemini-3.1-pro-preview` is preview** — could be less stable at tool-calling
  than GA; the hardened loop degrades rather than crashes, and the Gemini-rejects
  path falls back to Opus chat.
- **Billing assumption** — the design assumes the Google AI Studio Ultra sub
  covers `gemini-3.1-pro` API calls flat-rate. If the proxyd `gemini-api-key` is
  metered AI-Studio billing instead, tool turns incur per-token cost. **Bert to
  confirm before the live smoke;** not a code risk.
- **Findings flattening loses structure** — Opus sees prose, not typed results.
  Acceptable: Opus is voicing a summary for a human, not machine-parsing. The
  audit ledger remains the structured record.
