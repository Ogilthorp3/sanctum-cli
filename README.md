# sanctum-cli

> What `kubectl` is to Kubernetes — for Sanctum.

One terminal binary, `sanctum`, that is the unified front door to a Sanctum host. Routes prompts to the right model (Claude / Gemini / MLX-local), walks operators through painful provisioning flows (cloud backups, OAuth, credential rotation), reports honest health across the LaunchAgent constellation, and keeps every credential in the macOS Keychain.

## Status

**v0.9.0 — public release.** Installable from the public Homebrew tap. A person
who isn't the author can go from a fresh Mac to encrypted, verified cloud backups
in minutes with a single command.

| Capability | State |
|---|---|
| `brew install` from the public tap | ✅ |
| `sanctum onboard` — recipe → cloud wizard → first backup → restore canary | ✅ |
| Backup recipes (`family` / `operator` / `code`) + R2 / B2 / Google Drive backends | ✅ |
| Encrypted `restic` backups, per-host bucket, Keychain-only credentials | ✅ |
| Module manifest system + six-gate ship bar (`sanctum doctor --ship`) | ✅ |
| Honest, tier-aware `sanctum self-test` | ✅ |
| 13-pattern + filename pre-push secret scanner | ✅ |
| Rule-based prompt router (Claude / Gemini / MLX-local) | ✅ |
| Sigstore release signing | ⏳ roadmap |

See [`SPEC.md`](./SPEC.md) for the full design, doctrine, and roadmap.

## Install

```bash
brew tap ogilthorp3/sanctum
brew install sanctum-cli
sanctum --version
```

`restic` comes in as a recommended dependency; `rclone` is optional (Google Drive).

## Quick start

One command takes a fresh Mac to verified cloud backups. It scaffolds a minimal
`~/.sanctum/instance.yaml` if you don't have one, estimates your backup size against
the cloud free tier, walks you through the cloud credentials, runs the first backup,
and proves a restore round-trips a file through the cloud:

```bash
sanctum onboard --recipe family      # or: operator | code
```

Day to day:

```bash
sanctum status                       # one-line health: backup age, disk, providers
sanctum backup                       # run a backup
sanctum self-test                    # honest, tier-aware health check
sanctum doctor --ship backup         # score a module against the six ship-bar gates
```

## Develop

```bash
make venv && make install            # uv venv + editable install (Python 3.12)
make check                           # ruff + mypy + pytest
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

`sanctum-cli` reads `~/.sanctum/instance.yaml` (or `$SANCTUM_INSTANCE_FILE`). The file needs at minimum an `instance:` block with `name` + `slug` — `sanctum onboard` scaffolds a minimal one automatically on first run if you don't have it. The `cli:` block below is **optional**; every field has a sensible default.

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

## Module system — ship bar

Sanctum uses a module manifest system (`*.module.yaml`) to declare services, secrets, probes, alert routing, and uninstall steps. The ship bar gates each module before release.

```bash
sanctum module list                       # list discovered modules (builtin + user)
sanctum module status <name>              # gate summary for a module
sanctum module install <name>             # install a module (future)
sanctum module uninstall <name>           # uninstall a module (future)
sanctum module demo <name>                # run the module's demo command

sanctum doctor --ship <module>            # score a module against all six ship-bar gates
sanctum doctor --ship backup --json       # machine-readable JSON verdict + gate breakdown

sanctum soak <module> [--once]            # record one (or continuous) health samples
sanctum soak backup --days 7 --once       # single sample for use in cron
```

The six ship-bar gates are: `install/uninstall`, `secrets-bootstrap`, `self-heal`, `alert-hygiene`, `soak`, `docs+demo`. All six must be GREEN for a module to ship; AMBER is conditionally ready; RED blocks.

Module overrides in `instance.yaml`:

```yaml
cli:
  modules:
    backup:
      enabled: false   # disable a module without uninstalling it
```

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
