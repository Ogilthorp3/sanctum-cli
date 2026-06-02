# sanctum-cli — Design Specification

**Status:** v0 design draft · **Owner:** Bertrand Nepveu · **Last updated:** 2026-04-26

---

## 1. Mission

Provide one terminal binary, `sanctum`, that is the unified front door to a Sanctum host. It routes prompts to the right model, walks the operator through painful provisioning flows (cloud backups, agent setup, credential rotation), reports honest health across the local LaunchAgent constellation, and keeps every credential in the Keychain rather than on disk.

`sanctum` is what `kubectl` is to Kubernetes: a single tool that turns scattered scripts and a heterogeneous control plane into one composable CLI.

---

## 2. Design Doctrine

These are non-negotiable. Every feature is judged against them.

| Pillar | Means |
|---|---|
| **Closed-loop** | No silent partial states. Every operation either completes and verifies, or rolls back and reports. A backup that "succeeded" but didn't write a snapshot is a bug. |
| **Honest** | Telemetry reflects reality. If a provider rate-limits, the CLI says so — not "transient error." If a route was overridden, that's surfaced. No green checkmarks unless a real probe passed. |
| **Bounded** | Each command has a documented worst-case latency, network footprint, and cost. No unbounded retries. No surprise cloud charges. |
| **Defense-in-depth** | Credentials never on disk. Validation at boundaries (CLI input → schema, network → mTLS where available). Failures degrade gracefully through pre-defined fallbacks. |
| **Discovery-first** | No hardcoded IPs, ports, hostnames. Everything via `~/.sanctum/instance.yaml`, env override, or runtime discovery. (Per existing Sanctum doctrine.) |
| **Brevity by default** | `sanctum` with no args returns a one-liner status. Verbose only when something is wrong or `-v` is passed. (Per Jedi briefing brevity rule.) |
| **Python now, Rust later** | Build in Python with full type hints + tests. Promote hot paths (router, dispatcher) to Rust once the surface stabilizes. (Per Sanctum language maturity doctrine.) |

---

## 3. Architecture at a Glance

```
                      ┌─────────────────┐
                      │   sanctum CLI   │
                      └────────┬────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
   ┌────▼─────┐         ┌──────▼──────┐        ┌──────▼──────┐
   │  Router  │         │   Wizard    │        │   Doctor    │
   │ (chat,   │         │  (cloud,    │        │ (LaunchAgent│
   │ vision,  │         │ keychain,   │        │  health,    │
   │ code)    │         │  agent)     │        │  capacity)  │
   └────┬─────┘         └──────┬──────┘        └──────┬──────┘
        │                      │                      │
   ┌────▼──────────────────────▼──────────────────────▼─────┐
   │                  Provider abstraction                   │
   │   Claude · Gemini · MLX-local · Ollama (future)         │
   └────┬──────────────────────┬──────────────────────┬─────┘
        │                      │                      │
   ┌────▼─────┐           ┌────▼─────┐           ┌────▼─────┐
   │  Anthropic│           │  Google  │           │  127.0.0.1│
   │  via      │           │  AI      │           │  :8900    │
   │  proxy    │           │  Studio  │           │  sanctum- │
   │  (1234)   │           │          │           │  server   │
   └───────────┘           └──────────┘           └───────────┘

         ┌──────────────────────────────────────┐
         │  Cross-cutting: telemetry · config · │
         │  keychain · discovery                │
         └──────────────────────────────────────┘
```

Every arrow is loose-coupled. Providers are pluggable via a single ABC. The wizard module never imports providers directly — it uses the same dispatcher as `chat`.

---

## 4. CLI Surface

The shape is "noun → verb" where it makes sense, "verb → object" where the noun is implied. Tab completion via Typer.

```
sanctum                              # status one-liner (last backup, current route, alerts)
sanctum status [--json]              # detailed health snapshot

# Conversational dispatch
sanctum chat "..."                   # router decides; default Claude
sanctum chat -p gemini "..."         # force provider
sanctum chat -f file.txt "..."       # attach file (auto-detects type)
sanctum vision <image|video> "..."   # forced spatial → Gemini
sanctum code "..."                   # forced code → Claude
sanctum local "..."                  # forced MLX-local fallback

# Cloud backup lifecycle
sanctum cloud setup                  # TUI wizard (B2 / Drive / S3 / Storj / local NAS)
sanctum cloud status                 # last snapshot age, repo size, sync state
sanctum cloud verify [repo]          # restic check on one or all repos
sanctum cloud test                   # tiny canary backup + restore
sanctum cloud rotate                 # re-encrypt repo with new password
sanctum backup [--repo=local|cloud]  # run sanctum-backup.sh, stream output
sanctum backup snapshots             # list snapshots
sanctum backup restore <snap-id> <target>

# Operational
sanctum doctor                       # run all health probes; brevity-gated
sanctum doctor --full                # expanded diagnostic
sanctum doctor --fix                 # auto-remediate known drifts (mac-reconciler-style)
sanctum doctor --ship <module>       # score a module against the six ship-bar gates
sanctum doctor --ship <mod> --json   # machine-readable JSON verdict + gate breakdown
sanctum agent <name> [start|stop|status|logs]
sanctum proxy [restart|logs|status]
sanctum keychain [list|rotate <service>]

# Module system
sanctum module list                  # list discovered modules (builtin + user)
sanctum module status <name>         # gate summary for a module
sanctum module demo <name>           # run the module's demo command
sanctum module install <name>        # install a module
sanctum module uninstall <name>      # uninstall a module

# Soak harness
sanctum soak <module>                # continuous health recording loop
sanctum soak <module> --once         # single sample (for cron / launchd)
sanctum soak <module> --days 7       # target soak duration (informational)

# Meta
sanctum config validate              # schema-check ~/.sanctum/instance.yaml
sanctum config show <path>           # query a config path
sanctum version
sanctum self-update
```

**Exit codes** (every command honors these):

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | User error (bad input, missing flag, etc.) |
| 2 | Provider error (rate limit, auth, model unavailable) |
| 3 | Network error (DNS, connection) |
| 4 | Local error (Keychain, disk, missing dependency) |
| 5 | Configuration error (invalid instance.yaml) |
| 64+ | Reserved for future categories |

Scripts can branch reliably without grepping output.

---

## 5. Configuration Schema

All config lives in `~/.sanctum/instance.yaml`. The CLI adds a top-level `cli:` section. The schema is validated on every load with **pydantic v2**; a malformed config aborts with a precise pointer to the offending key.

```yaml
cli:
  default_provider: claude
  
  routing:
    rules:
      - when: { has_image: true }
        then: gemini
      - when: { has_video: true }
        then: gemini
      - when: { intent: spatial }
        then: gemini
      - when: { intent: code }
        then: claude
      - when: { offline: true }
        then: mlx_local
    fallback: claude
  
  providers:
    claude:
      via: proxy
      proxy_endpoint: http://127.0.0.1:1234   # discovered, not hardcoded
      model: claude-opus-4-7
      keychain:
        service: anthropic-api-key
        account: bert
      timeout_s: 120
      max_retries: 2
    
    gemini:
      api_endpoint: https://generativelanguage.googleapis.com/v1beta
      model: gemini-2.5-pro
      keychain:
        service: google-ai-api-key
        account: bert
      timeout_s: 120
      max_retries: 2
    
    mlx_local:
      endpoint: http://127.0.0.1:8900   # sanctum-server
      model: Qwen3.5-27B-4bit
      timeout_s: 60
      always_available: true   # used as offline fallback
  
  telemetry:
    enabled: true
    path: ~/.sanctum/telemetry/cli.jsonl
    redact_prompts: true       # default: never log prompt content
    aggregate_window_days: 7
  
  cloud_backup:
    primary:
      kind: restic
      repo: /Volumes/T9/sanctum-restic
      keychain:
        service: sanctum-backup-key
        account: sanctum-backup
    secondary:
      kind: restic
      repo: rclone:gdrive-sanctum:sanctum-restic
      keychain:
        service: sanctum-backup-key
        account: sanctum-backup
    retention:
      keep_daily: 7
      keep_weekly: 4
      keep_monthly: 12
  
  ui:
    color: auto             # auto | always | never (also honors NO_COLOR)
    progress: auto          # auto | rich | none
    json_default: false
```

Discovery overrides every literal: any field can be replaced by `${env:VAR}` or `${discover:service.endpoint}`. The router resolves these at load time. **No literal hostnames or ports in code, ever.**

---

## 6. Routing Logic

The router is a **pure function**:

```python
def route(intent: Intent, attachments: list[Attachment], 
          flags: Flags, config: Config) -> Provider:
    # 1. Explicit flag wins
    if flags.provider:
        return config.providers[flags.provider]
    
    # 2. Subcommand-implied intent (vision/code/local)
    if intent.implied:
        return config.providers[intent.implied]
    
    # 3. Content-based rules (first match wins)
    for rule in config.routing.rules:
        if rule.matches(intent, attachments):
            return config.providers[rule.then]
    
    # 4. Connectivity gate
    if not network_available() and config.providers.mlx_local:
        return config.providers.mlx_local
    
    # 5. Fallback
    return config.providers[config.routing.fallback]
```

Pure function = easy to property-test. Every dispatch decision is logged to telemetry with the rule that fired (`route.rule = "has_image"`), so the operator can audit why a request went where.

**Override hierarchy** (highest wins): CLI flag → env var (`SANCTUM_PROVIDER=...`) → instance.yaml → built-in default.

---

## 7. Provider Abstraction

```python
class Provider(ABC):
    name: str
    capabilities: set[Capability]   # CHAT | VISION | TOOLS | STREAMING | THINKING

    @abstractmethod
    async def chat(self, messages: list[Message], 
                   attachments: list[Attachment], 
                   opts: ChatOpts) -> AsyncIterator[Chunk]: ...
    
    @abstractmethod
    async def health(self) -> HealthSnapshot: ...
    
    @abstractmethod
    def cost(self, usage: Usage) -> Decimal: ...
```

Implementations:
- `ClaudeProvider` — POSTs to `claude-cli-proxy` (already running locally on `:1234`). Uses Anthropic streaming format.
- `GeminiProvider` — direct Google AI Studio API.
- `MlxLocalProvider` — POSTs to `sanctum-server` on `:8900` (OpenAI-compat).
- Future: `OllamaProvider`, `OpenRouterProvider`.

**Capability gating**: if a provider lacks `VISION` and the router selects it for an image, the dispatcher errors *before* the request goes out — no half-broken multipart upload to a text-only model.

---

## 8. Cloud-Setup Wizard

The wizard is a **Textual** app (`sanctum cloud setup`). Each backend is a **state machine** of small screens; every screen has explicit success and failure transitions. State persists in `~/.sanctum/wizard-state.json` so the wizard is **resumable** if the operator quits or the laptop sleeps mid-OAuth.

### Wizard architecture

```
┌─────────────────────────────────────────────────┐
│  Welcome / detect existing setup                │
│  → Resume? Reconfigure? Add second target?      │
└────────────────┬────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────┐
│  Choose backend                                 │
│  [B2]  [Storj]  [S3]  [Drive]  [Local NAS]      │
│  Recommendation badge on the easiest path       │
└────────────────┬────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────┐
│  Per-backend script (states 1..N below)         │
└────────────────┬────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────┐
│  Connection canary (write+read 1 KB)            │
└────────────────┬────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────┐
│  Round-trip test (snapshot → restore → diff)    │
└────────────────┬────────────────────────────────┘
                 ▼
┌─────────────────────────────────────────────────┐
│  Persist to instance.yaml + Keychain            │
│  Show resulting `sanctum cloud status`          │
└─────────────────────────────────────────────────┘
```

Every screen has a **"Why am I doing this?"** disclosure (single keystroke) so the operator never feels in a black box. Every failure renders a **structured error** with `cause`, `suggested_fix`, and `[r]etry / [s]kip / [q]uit` actions.

### Per-backend wizard scripts

#### Backblaze B2 (recommended default — easiest path)

| Step | Wizard does | User does |
|---|---|---|
| 1 | Opens `https://www.backblaze.com/sign-in` | Logs in (or signs up — wizard waits) |
| 2 | Opens `https://secure.backblaze.com/app_keys.htm` with overlay text "Click 'Add a New Application Key'. Name it `sanctum-restic`. Allow access: All buckets. Capabilities: Read+Write." | Creates the key, copies values |
| 3 | Renders two fields with regex live-validation: `keyID` (^[0-9a-f]{25}$), `applicationKey` (^K00[0-9a-zA-Z+/]{40,}$) | Pastes both |
| 4 | `b2 authorize-account` test (bin shipped with wizard) | – |
| 5 | Auto-creates bucket `sanctum-restic-${hostname}-${random_suffix}` | – |
| 6 | Stores keys in Keychain (`b2-account-id`, `b2-application-key`) | – |
| 7 | `restic init` against `b2:bucket-name:/` with passphrase from `sanctum-backup-key` | – |
| 8 | Canary write+read | – |
| 9 | Mini round-trip: `restic backup ~/.zshrc; restic restore latest --target /tmp/sanctum-test; diff` | – |
| 10 | Persists to `instance.yaml`. Done. | – |

**Realistic time-to-success: 2–3 minutes.**

#### Google Drive (escape hatch — privacy-preserving but painful)

The wizard *cannot* skip Google's verification gate, but it can collapse the 7-step click-ops nightmare into a guided flow. Each Cloud Console step opens at a deep-link, the wizard says "wait until you see X, then press Enter." Live-validates pasted client_id/secret format. Auto-runs `rclone config update + reconnect`.

The OAuth "publish app" step is automated as a single toggle in the wizard ("Make refresh tokens permanent? [Y/n]"); on Y, the wizard opens the publishing-status URL with explicit "click 'Publish App'" instructions.

**Realistic time-to-success: 8–10 minutes** (down from 90 in the manual path we just survived).

#### Storj, S3-compatible, Local NAS

Each follows the same B2-style pattern: API-key paste → canary → init → round-trip → persist. Local NAS just substitutes a `sftp:` rclone backend or a directly-mounted volume.

#### iCloud / OneDrive

Wizard explicitly **refuses** these as primary backup targets. iCloud has no API for a backup tool; OneDrive throttles aggressively for restic-style small-pack workloads. They are listed but greyed out with a `(?)` explanation. (Aligns with vendor preferences memory.)

### Pre-flight checks (run before any wizard step)

The wizard refuses to start until:

- [ ] `restic --version` ≥ 0.18
- [ ] `rclone --version` ≥ 1.73 (only for cloud backends that need it)
- [ ] Keychain entry `sanctum-backup-key` exists (or wizard offers to generate)
- [ ] Target volume reachable / mounted
- [ ] `instance.yaml` is schema-valid

If any check fails, the wizard offers to fix it (`brew install restic` etc.) before proceeding.

---

## 9. Telemetry & Observability

Every command emits a JSONL event to `~/.sanctum/telemetry/cli.jsonl`. Schema:

```json
{
  "ts": "2026-04-26T22:14:18-04:00",
  "command": "chat",
  "subcommand": null,
  "provider": "claude",
  "route_rule": "fallback",
  "intent": "general",
  "duration_ms": 4218,
  "tokens_in": 312,
  "tokens_out": 1140,
  "cost_usd": 0.0142,
  "exit_code": 0,
  "error": null,
  "host": "Berts-Mac-Mini-M4-Pro"
}
```

**Prompt content is never logged by default.** A `redact_prompts: false` config flag enables full content logging for development. The CLI prints a startup banner whenever full logging is on.

**Aggregation**: `sanctum status` shows last 7d totals (requests, tokens, $, error rate). Holocron sidecar tails the JSONL and renders a panel.

**Retention**: log file rotates at 10 MB or 30 days, whichever first. Old files compressed to `.jsonl.zst`.

---

## 10. Security Model

| Concern | Mitigation |
|---|---|
| API keys on disk | Never. All credentials in macOS Keychain. CLI reads at invocation; never caches. |
| Config file with secrets | `instance.yaml` references Keychain entries by name only. `chmod 600`. Schema-validated on every load. |
| Telemetry leaking prompts | Redacted by default. Opt-in only. Banner when off. |
| Provider impersonation | mTLS where available (sanctum-server uses it). HTTPS pinning for Anthropic/Google. |
| Replay attack on cached responses | All responses streamed live, no cache shorthand by default. Optional response cache uses content-hashed keys. |
| Wizard state file leak | `~/.sanctum/wizard-state.json` is `chmod 600`, contains no secrets (only "step 4 of B2 wizard, awaiting paste"). |
| Self-update tampering | `sanctum self-update` verifies a Sigstore/cosign signature on the new binary before swapping. |
| Compromised PATH binary | At launch, `sanctum` self-checks via fixed install path; refuses to run from world-writable dirs. |

The Keychain is the single trust anchor. Lose it, lose everything — same as the existing restic model.

---

## 11. Failure Modes & Recovery

Every failure mode is enumerated, has a deterministic recovery path, and is exercised by integration tests.

| Failure | Detection | Recovery |
|---|---|---|
| Network down | Initial DNS check (~50ms) | Auto-route to `mlx_local`. CLI warns once per session. |
| Provider rate limit | Provider returns 429 | Single retry with backoff. If second 429: surface to user with "fall back to Y? [y/N]". |
| Provider auth fail | 401/403 | Open Keychain entry path; suggest `sanctum keychain rotate`. Do not retry. |
| Bad config | pydantic ValidationError on load | Print path-to-bad-key, current value, expected schema, exit 5. |
| Repo corrupt | `restic check` fails | `sanctum cloud doctor` offers `restic rebuild-index` and snapshot of remaining good data. Never auto-deletes. |
| Wizard interrupted mid-OAuth | Wizard state on disk | Resume from last completed step on next launch. Idempotent. |
| Disk full during backup | `restic backup` errors with ENOSPC | Surface clearly. Suggest `sanctum cloud forget` (prune) or external offload. **Never** auto-prune as a side effect. |
| Both providers rate-limited | Both return 429 in 30s window | Queue request locally with timeout; user can `sanctum chat --queue` to opt in. |
| Keychain locked | `security` exits 36 | Prompt for unlock via `osascript -e 'tell app "Keychain Access" to unlock'`. |

**Closed-loop guarantee**: every operation publishes a `result` event. The absence of a `result` event for a started operation is itself an alarm condition picked up by `sanctum doctor`.

---

## 12. Roadmap

### v0.1 — MVP (target: 2 weekends)

- [ ] `sanctum chat "..."` via existing `claude-cli-proxy`
- [ ] `sanctum vision <file> "..."` direct to Gemini
- [ ] `sanctum cloud setup` — wizard for **B2 + Drive only**
- [ ] `sanctum status` — one-liner
- [ ] `sanctum config validate`
- [ ] `cli:` section in `instance.yaml` with pydantic schema
- [ ] Telemetry to JSONL (redacted by default)
- [ ] Keychain integration (existing `sanctum-backup-key` reuse)
- [ ] Tab completion (Typer ships this for free)
- [ ] Test suite: router (pure, property-tested), config validator, wizard via Textual pilot

**Acceptance**: a fresh Mac can go from zero to a working backup in ≤ 5 minutes with B2.

### v0.2 — Operations (target: 1 weekend)

- [ ] `sanctum doctor` (probes every `com.sanctum.*` LaunchAgent)
- [ ] `sanctum backup [run|verify|restore|snapshots]`
- [ ] `sanctum agent <name> [start|stop|status|logs]`
- [ ] `sanctum proxy [restart|logs|status]`
- [ ] Holocron telemetry panel reading `cli.jsonl`
- [ ] `--json` flag on every command

### v0.3 — Resilience (target: 1 weekend)

- [ ] Local MLX fallback when network down
- [ ] Provider rate-limit awareness with explicit user-facing fallback prompt
- [ ] Storj + S3 + Local NAS in the wizard
- [ ] `sanctum keychain rotate <service>`
- [ ] `sanctum cloud rotate` (re-encrypt repo with new password)

### v0.4 — Power-user surface

- [ ] TUI dashboard (`sanctum dashboard`) — live tail of telemetry + LaunchAgent state
- [ ] Routing rule live-edit (`sanctum config edit cli.routing`)
- [ ] Provider cost budget alerts ("you've spent $X today")
- [ ] Multi-host coordination via existing `sanctum-presence` (`sanctum --on manoir doctor`)

### v1.0 — Hardening

- [ ] Rust port of the dispatcher (`sanctum chat` cold-start < 50 ms)
- [ ] Single static binary distribution (still calls Python TUI for wizards via subprocess)
- [ ] Brew formula
- [ ] Sigstore-signed releases
- [ ] Optional: PyPI publish if going public

---

## 13. Testing Strategy

| Layer | Tool | Coverage target |
|---|---|---|
| Router (pure) | hypothesis (property tests) | 100% branch |
| Config validator | pydantic + pytest | 100% |
| Provider clients | pytest + respx (HTTP mocking) | 90% |
| TUI screens | Textual `Pilot` fixture | 80% (every screen reachable, every error path exercised) |
| Wizard end-to-end | Real B2 test bucket via env-gated CI | Smoke test only |
| CLI surface | pytest-shell (subprocess invoke) | All exit codes covered |

Every PR runs the full suite. Wizard E2E is gated on a CI-only B2 account so contributors don't need credentials.

**Chaos tests** (recurring, not per-PR):
- Kill restic mid-snapshot — verify no corruption, lock cleanup works
- Yank network during `sanctum chat` — verify graceful fallback
- Corrupt `instance.yaml` mid-load — verify clear error + non-zero exit
- Fill disk during backup — verify ENOSPC handling
- Lock Keychain during invocation — verify unlock prompt

---

## 14. Open Questions

1. **Streaming format for `sanctum chat`** — raw stdout, or render through a small markdown renderer (Rich)? Probably raw stdout by default, `--render` opt-in; Rich's heuristics sometimes break code blocks.

2. **Cost ceilings** — should v0.1 have a hard $/day cap that refuses to dispatch? Probably yes for production-grade, but adds UX friction. Defer to v0.4.

3. **MCP integration** — sanctum-cli could expose its commands as MCP tools so other Claude/Gemini sessions can call them. Powerful but expanded blast radius. Defer.

4. **Multi-tenant** — DenchClaw vs OpenClaw vs the VM each have their own surface. v1.0+ should let `sanctum --on yoda doctor` work via Tailscale + sanctum-presence. Out of v0.x scope.

5. **The Rust crossover point** — when does Python pain exceed rewrite cost? Likely when cold-start hits >200 ms or when the dispatcher needs concurrency primitives Python can't give cleanly. Watch and decide; don't pre-optimize.

6. **Sanctum the product** — if this ever ships beyond manoir, the wizard's pre-flight bundles must include `restic`/`rclone` install (probably via a static-binary downloader, not brew, for portability). Keep this in mind even in v0.1 — don't build a brew dependency into the core logic.

---

## Appendix A — Comparison to existing scripts

| Existing | Replaced by |
|---|---|
| `~/Backups/sanctum-backup.sh` | `sanctum backup run` (script becomes thin shim that calls into the Python module — preserves cron/launchd compat) |
| `~/.sanctum/scripts/claude-cli-proxy.js` | Stays — `sanctum chat` is a client of the proxy, not a replacement |
| `~/.sanctum/bin/sanctum-triage` | `sanctum doctor` (eventually) |
| Various `*-tunnel` LaunchAgents | `sanctum agent <name>` for control plane; agents themselves stay independent |
| `tools/audit_runtime_launchagents.py` | `sanctum doctor --full` includes this pass |

Migration is progressive. Existing scripts stay working through v0.x; the CLI is additive, not destructive.

---

## Appendix B — Why not just bash?

Bash gets us 60% there; the breaking points are:

- **Schema-validating `instance.yaml`** — `yq` works but doesn't catch type mismatches before runtime
- **Wizard state machines** — possible in bash (we've seen `gum`) but not maintainable past ~5 screens
- **Async streaming for `sanctum chat`** — bash + `curl --no-buffer` works for one stream but falls apart with parallel providers / retries / fallback
- **Test surface** — bash testing is possible (`bats`) but property tests, mocks, and TUI fixtures all need a real language

Python with type hints + pydantic + Typer + Textual covers all of these without sacrificing the "small CLI" feel. The eventual Rust port stays scoped to the dispatcher hot path, not the wizard, so this isn't wasted work.

---

## Appendix C — Glossary

- **Route** — a routing decision: which provider receives a given request.
- **Intent** — the kind of work being requested (chat, vision, code, spatial).
- **Capability** — what a provider can do (CHAT, VISION, TOOLS, STREAMING, THINKING).
- **Wizard** — a Textual TUI flow with explicit state machine, resumability, and round-trip verification.
- **Doctor** — a diagnostic pass that probes Sanctum's LaunchAgents, capacity, repos, and surfaces drifts.
- **Closed-loop** — every started operation must produce a terminal result event (success or failure with cause).
