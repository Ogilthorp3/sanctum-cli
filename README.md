# sanctum-cli

> What `kubectl` is to Kubernetes — for Sanctum.

One terminal binary, `sanctum`, that is the unified front door to a Sanctum host. Routes prompts to the right model (Claude / Gemini / MLX-local), walks operators through painful provisioning flows (cloud backups, OAuth, credential rotation), reports honest health across the LaunchAgent constellation, and keeps every credential in the macOS Keychain.

## Status

**v0.1.0a1 — bootstrap.** Foundation in place. One end-to-end command (`sanctum status`) and one config command (`sanctum config validate`). Provider implementations and the cloud-setup TUI wizard are deferred to v0.2 / v0.3 per `SPEC.md`.

| Layer | State |
|---|---|
| Config schema (pydantic v2) | ✅ |
| Discovery resolver (env > config > default) | ✅ |
| Keychain wrapper (macOS `security`) | ✅ |
| Telemetry (JSONL, redacted by default) | ✅ |
| Pure router (rule-based, property-tested) | ✅ |
| Provider ABC + Capability flags | ✅ stub |
| `sanctum status` (host, disk, backups, telemetry) | ✅ |
| `sanctum config validate` | ✅ |
| Provider implementations | ⏳ v0.2 |
| Cloud-setup TUI wizard | ⏳ v0.3 |
| Doctor / agent / proxy commands | ⏳ v0.2 |

## Read first

- [`SPEC.md`](./SPEC.md) — full design (548 lines): mission, doctrine, architecture, CLI surface, config schema, routing, providers, wizards, security, failure modes, roadmap, testing.

## Quick start (dev)

```bash
make venv          # uv venv .venv (Python 3.12)
make install       # uv pip install -e ".[dev]"
make check         # ruff + mypy + pytest
make run           # sanctum status
```

## Doctrine

Codified in `SPEC.md`. Summary:

- **Closed-loop** — every operation either completes and verifies or rolls back and reports.
- **Honest** — telemetry reflects reality. No silent partial success.
- **Bounded** — each command has a documented worst-case latency and cost.
- **Defense-in-depth** — credentials only in Keychain, never on disk.
- **Discovery-first** — no hardcoded IPs/ports/hostnames; everything via `instance.yaml` or `SANCTUM_*` env.
- **Brevity by default** — `sanctum` with no args returns a one-liner.
- **Python now, Rust later** — Python prototype, Rust dispatcher port at v1.0+.

## Layout

```
sanctum_cli/
├── cli.py            # Typer entry
├── config.py         # pydantic schema + loader
├── discovery.py      # env > config > default
├── keychain.py       # macOS Keychain wrapper
├── telemetry.py      # JSONL append-only emitter
├── router.py         # pure routing function
├── errors.py         # exit-code taxonomy
├── commands/         # one module per subcommand
│   ├── status.py
│   └── config_cmd.py
├── providers/        # ABC + concrete impls (v0.2)
│   └── base.py
├── tui/              # Textual wizards (v0.3)
└── backends/         # cloud-backup backends (v0.3)
```

## Configuration

`sanctum-cli` reads `~/.sanctum/instance.yaml` (or `$SANCTUM_INSTANCE_FILE`) and pulls a `cli:` block. The block is **optional** — every field has a sensible default. Customize when you want to.

```yaml
instance:
  name: My Sanctum
  slug: my-sanctum

cli:
  default_provider: claude
  routing:
    rules:
      - when: { has_image: true }
        then: gemini
      - when: { intent: code }
        then: claude
    fallback: claude
  telemetry:
    enabled: true
    redact_prompts: true
  cloud_backup:
    primary:
      kind: restic
      repo: /Volumes/T9/sanctum-restic
      keychain:
        service: sanctum-backup-key
        account: sanctum-backup
```

`sanctum config validate` checks the schema and prints a precise pointer for any violation.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | User error (bad input, missing flag) |
| 2 | Provider error (rate limit, auth, model) |
| 3 | Network error (DNS, connection) |
| 4 | Local error (Keychain, disk, missing dependency) |
| 5 | Configuration error (invalid `instance.yaml`) |

Scripts can branch on `$?` without parsing output.
