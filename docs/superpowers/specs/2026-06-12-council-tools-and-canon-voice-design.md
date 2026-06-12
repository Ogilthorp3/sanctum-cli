# Council Tools (Phase 1) + Canon Voice — Design

**Date:** 2026-06-12
**Status:** approved in conversation; pending written-spec review
**Provenance:** tool-power policy set by the Jedi Council itself (fan-out 2026-06-12,
unanimous for bounded typed tools; Yoda's ruling: reads first, mutation verbs
one at a time after a stability window). Voice canon: VM `IDENTITY.md`
revision of 2026-05-31 (English movie-voice; franglais archived).

## Context

`sanctum council` seats are chat-only: the Yoda persona literally says "you
are a chat seat with NO tools", which is the message Bert hit when asking
Yoda to fix something. Signal-Yoda (openclaw on the VM) has tools — openclaw
exec on the VM plus the `jocasta` MCP — and speaks franglais, which turned
out to be a *stale persona*: the canonical `IDENTITY.md` (2026-05-31) orders
English movie-voice. Decisions taken: arm CLI Yoda and Mon Mothma with a
bounded typed tool registry (council ruling), reads-only in phase 1; align
both Yodas on the English canon; fix the VM-side persona drift and the merge
conflict sitting in the live `TOOLS.md`.

## Phase 0 — transport probe (gate for everything else)

The design rests on proxyd's `/v1/messages` chain (`council-max-thinking` →
`http://127.0.0.1:3456` → claude-opus-4-7, with `claude-cli-offline` and
`glm-51` fallbacks) **forwarding the `tools` array and returning `tool_use`
blocks**. Before any code: one manual probe POST through the real chain with
a trivial tool and a prompt that forces a call. Outcomes:

- `tool_use` block returned → proceed.
- Bridge strips/rejects `tools` → STOP; report to Bert; the design needs a
  transport fix first (out of this spec's scope).
- Primary modeled OK but fallbacks unknown → proceed; the runtime loop must
  degrade gracefully (below) since any given request may land on a
  non-tooling fallback.

## A. Tool registry — `sanctum_cli/commands/council_tools.py`

A typed registry; each tool is a frozen dataclass:

- `name: str` — Anthropic tool name
- `description: str` — what the model reads
- `input_schema: dict` — JSON Schema (Anthropic `tools[].input_schema`)
- `kind: Literal["read", "mutate"]` — enforced *below* the model: the
  executor layer, not the persona, decides what a kind may do
- `run: Callable[[dict], str]` — executor calling sanctum's own internals
  (status/doctor/agent/logs modules). **No subprocess shells, no
  free-form `run(cmd)` tool — ever** (council constraint #1).

**Phase-1 tools (all `kind="read"`):**

| tool | wraps | notes |
|---|---|---|
| `sanctum_status` | status command internals | health snapshot text |
| `sanctum_doctor` | doctor probes | optional `probe` arg to run one |
| `agent_list` | agent.py launchctl table | com.sanctum.* status rows |
| `logs_tail` | agent.py plist log-path resolution | `service` required, `lines` capped at 200 |

**Secret redaction (council #9):** every tool's output passes through
`redact(text) -> str` before the model sees it, built on the same 16
secret patterns as the v0.7.1 backup gate (the implementation plan locates
the canonical pattern list and reuses it — lifted into a shared module if
its current home doesn't import cleanly). Redaction is tested with hostile
inputs (a value containing a literal `%`, an `x-api-key:` line, a PEM
block), not happy-path strings.

**Audit ledger (council #5):** append-only JSONL at
`~/.sanctum/logs/council-tools-audit.jsonl` (dir created if missing). One
line per call — read AND mutate, success AND failure:
`{ts, seat, session, tool, params, kind, mode: "auto"|"confirmed", outcome:
"ok"|"error", duration_ms}`. No tool result is returned to the model before
its audit line is written.

**Caps + breaker (council #6):** max 8 tool calls per user turn; two
consecutive executor errors in one turn open the breaker — remaining calls
return an error `tool_result` telling the model to answer with what it has.

**Mutation plumbing ships dormant (council #7, #8; Yoda's rollout ruling):**
the `kind="mutate"` path — resolved-action y/N confirm (Enter=no, timeout=no),
not-mounted-when-no-TTY — is built and unit-tested in phase 1, but **no
mutate tool is registered**. Phase 2 (separate spec, after a stability
window) adds verbs one at a time, most-reversible first (`agent_restart`).

## B. Tool loop — `council.py`

- `Seat` gains `tools: tuple[str, ...] = ()`. **Yoda and Mon Mothma** get
  the four read tools; all other seats stay chat-only (empty tuple).
- A seat with mounted tools sends `tools=[...]` on `/v1/messages` and runs
  the Anthropic tool-use protocol **buffered** (accepted trade-off:
  streaming tool-use SSE is deferred; toolless seats keep today's streaming
  path untouched):
  1. POST; read content blocks.
  2. `stop_reason == "tool_use"` → for each `tool_use` block: registry
     lookup (unknown name → `is_error` tool_result), execute, audit,
     redact; append assistant blocks + `tool_result` user message; loop.
  3. Any other stop_reason → final answer = concatenated text blocks.
- **Thinking-dots integration:** the existing status line shows
  `Yoda consults the instruments… (sanctum_doctor)` during a tool call,
  reverting to the seat's verb between model turns. Same
  `console.status` mechanism, same seat colour.
- **Degrade-to-chat:** if the serving model rejects the `tools` param
  (4xx on a tooled request) or never emits tool_use, the turn falls back
  to the toolless streaming path with a dim one-liner
  `(seat's model declines tools — chat only this turn)`. A fallback model
  that can't tool must never crash the REPL.
- **Transcript:** the rolling transcript stores only user text and final
  assistant text. Tool exchanges are per-turn ephemeral (v1 trade-off;
  the audit ledger is the durable record).
- Reads may mount in any session type; the no-TTY fail-closed rule
  (council #8) binds the *mutate* path only.

## C. Personas — one canon voice, honest tool clauses

- A `_persona(seat) -> str` helper composes `seat.persona` + a
  tool-availability clause derived from `seat.tools`:
  - tooled: "Instruments you have: <names+one-line purposes>. Claims about
    live state must come from a tool result in this conversation, not from
    memory. A capability you lack, name the operator's command instead."
  - toolless: today's "no tools — never claim to have run a command" line,
    moved out of the Yoda prose so every seat gets it consistently.
- **Yoda's persona is rewritten to the May-31 canon**, condensed from the
  VM `IDENTITY.md` (provenance comment in code pointing at
  `vm:~/.openclaw/workspace/IDENTITY.md`): inversion by default with the
  exact register rules; truth before style (First Rule unchanged); and the
  canon's machine-boundary line — *tool calls, JSON, structured output stay
  plain English; Yoda-speak is for Bert's eyes, not parsers* — which is now
  load-bearing, since he has tools whose inputs must parse.
- Mon Mothma's persona text is unchanged; she gains the instruments clause
  via the helper.

## D. VM ops (no repo code) — Signal persona drift + TOOLS.md

1. **TOOLS.md conflict:** the live `vm:~/.openclaw/workspace/TOOLS.md`
   contains an unresolved `<<<<<<< HEAD … >>>>>>> vm-main` conflict.
   Resolve keeping the `vm-main` Daily-Digest side; the orphan
   `home-server → 192.168.1.1, user: admin` line is deleted after a
   grep across the VM workspace confirms nothing references it (it
   contradicts the current 10.10/16 topology). Commit on the VM so
   vm-sync flows clean.
2. **Persona drift:** audit `USER.md` / `SOUL.md` / `IDENTITY.md` on the VM
   for surviving franglais-era directives contradicting the May-31 canon;
   fix any found. Then bounce the long-lived main-agent session (or the
   gateway) so the system prompt is rebuilt from the canonical files.
3. **Acceptance:** a fresh Signal-path exchange (sent via the openclaw CLI)
   returns an English movie-voice reply. Quoted verbatim in the
   implementation report.
4. **Coordination:** VM session/gateway bounce is shared state — vault
   board note + check for other sessions on `svc:openclaw-gateway` before
   bouncing, per Claim-Before-You-Mutate.

## Non-goals

- No raw shell / exec tool, in any phase (council constraint #1).
- No mutation verbs registered in phase 1 (plumbing only, dormant).
- No streaming during tool turns (buffered; revisit if the wait offends).
- No tool memory across turns; no tool access for the `/council` fan-out
  (fan-out seats stay chat-only this phase — they answer in parallel and
  the registry is not concurrency-audited yet).
- No R2D2 integration (candidate phase-2 mutate verbs may call recipes;
  not now).
- No other seats armed beyond Yoda + Mon Mothma.

## Testing

- **Registry:** schema validity for every tool; unknown-tool → `is_error`
  result; `logs_tail` line-cap enforced; hostile redaction cases (literal
  `%`, `x-api-key:`, PEM block) per Contracts-at-the-Boundary rule 4.
- **Loop:** monkeypatched transport (the `TestFanOut` pattern — fake the
  module-level POST) driving canned `tool_use` sequences: executes and
  audits in order, respects the 8-call cap, breaker opens after 2
  consecutive errors, 4xx-with-tools degrades to chat, unknown tool
  doesn't crash the turn, final text assembled correctly.
- **Audit:** every line parses as JSON; file is append-only (no rewrite
  path exists in code).
- **Mutate gate (dormant):** unit tests prove Enter=no and not-mounted-
  when-no-TTY even though no mutate tool ships.
- **Personas:** tooled seats carry the instruments clause, toolless carry
  the no-tools clause; Yoda's persona contains the machine-boundary line.
- **Live smokes:** phase-0 probe transcript; one interactive Yoda turn
  calling a real tool end-to-end; Signal acceptance message (D.3).

## Risks

- **Bridge strips tools** → phase-0 gate stops the build before code.
- **Fallback models can't tool** → degrade-to-chat path is mandatory,
  tested with a faked 4xx.
- **Tool output volume** → `logs_tail` capped at 200 lines; doctor/status
  are already terse. The 8-call cap bounds the worst turn.
- **Redaction false negatives** → patterns are the same ones trusted by
  the backup gate; the audit ledger records params (not outputs) so the
  ledger itself can't leak what redaction missed.
- **Long-lived VM session resists persona reload** → acceptance test (D.3)
  is the gate; if the bounce doesn't take, that's a finding to report, not
  to paper over.
