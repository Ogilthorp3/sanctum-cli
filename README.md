# sanctum-cli

> What `kubectl` is to Kubernetes — for Sanctum.

One terminal binary, `sanctum`, that is the unified front door to a Sanctum host. Routes prompts to the right model (Claude / Gemini / MLX-local), walks operators through painful provisioning flows (cloud backups, OAuth, credential rotation), reports honest health across the LaunchAgent constellation, and keeps every credential in the macOS Keychain.

## Status

**v0.15.4 — public beta.** Installable from the public Homebrew tap. A person
who isn't the author can go from a fresh Mac to encrypted, verified cloud backups
in minutes with a single command.

The beta-safe command set runs on any Mac:

| Command | What it does | State |
|---|---|---|
| `sanctum status` | One-line health: backup age, disk, providers | ✅ |
| `sanctum init` | Scaffold a minimal `~/.sanctum/instance.yaml` | ✅ |
| `sanctum onboard` | recipe → cloud wizard → first backup → restore canary | ✅ |
| `sanctum doctor` / `--ship` | Health probes + six-gate module ship bar | ✅ |
| `sanctum self-test` | Honest, tier-aware health check | ✅ |
| `sanctum backup` | Recipes (`family` / `operator` / `code`) over R2 / B2 / Google Drive | ✅ |
| `sanctum cloud setup` | Guided cloud-backend wizard | ✅ |
| `sanctum net check / optimize / speedtest` | NAT/DMZ topology wizard + bandwidth probe | ✅ |
| `sanctum config validate` | Schema-check `instance.yaml` with a precise pointer | ✅ |
| `sanctum keychain` / `keys backup` | Inspect/rotate + export Keychain-only credentials | ✅ |
| `sanctum module` / `logs` / `update` | Module manifests, log tail, brew-gated self-update | ✅ |
| Sigstore release signing | Release artifact signing | ⏳ roadmap |

Some commands need a **full Sanctum haus** (the Mini + Firewalla + council) and
are not part of the beta — `brainstorm`, `council`, `chat`, `code`, `bridge`,
`proxy`, `agent`, `screen-time`, `devices`, `schedule`, `endocrine`. Running one
without the haus prints a short banner and exits cleanly; it never half-runs. See
[sanctum.run](https://sanctum.run) for the full setup.

See [`SPEC.md`](./SPEC.md) for the full design, doctrine, and roadmap.

## Install

```bash
brew install ogilthorp3/sanctum/sanctum-cli
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
sanctum link status                  # Wi-Fi link health + IDENTITY verdict (is this node quarantined?)
sanctum link optimize                # audit Wi-Fi identity; --apply pins a stable MAC (servers), --verify confirms
```

### Link Identity Guard — stay on Wi-Fi, unquarantined

A fixed-infra Mac on Wi-Fi can be fully associated yet unreachable on the LAN: macOS
re-defaults "Private Wi-Fi Address" to Rotating on every network re-join, so the node
keeps presenting a *changing* MAC — and any router that keys trust to a MAC (a DHCP
reservation, a device allow-list, a Firewalla quarantine tag) stops recognizing it. The
failure looks perfect to radio diagnostics (RSSI/BSSID are fine); it lives one layer up,
at *who the node is on the network*.

`sanctum link` detects that exact signature (router-agnostic), auto-classifies the node
(a SERVER-class node is enrolled on the home SSID; a roaming laptop keeps its private MAC),
and — with your one-click approval — enforces a **stable hardware MAC via a per-SSID
configuration profile** so macOS can't silently re-randomize it. `sanctum onboard` sets it
up automatically for fixed-infra nodes. Fail-closed, per-SSID (home only), never touches a
roaming laptop's privacy without `--force`.

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
├── tui/              # reserved for future rich-based UI helpers
└── backends/         # cloud-backup backends + rich-prompt wizards
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

## License

FSL-1.1-MIT (converts to MIT after 2 years) — see [`LICENSE`](./LICENSE). The
Functional Source License lets you use, modify, and redistribute the software
for any purpose except a competing commercial product; two years after each
release that version is additionally available under the MIT License.

## Security

Found a vulnerability? See [`SECURITY.md`](./SECURITY.md) for how to report it
privately. Please do not open a public issue for security problems.
