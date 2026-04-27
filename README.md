# sanctum-cli

**Status:** design — not yet implemented

One terminal binary, `sanctum`, that is the unified front door to a Sanctum host. Routes prompts to the right model (Claude / Gemini / MLX-local), walks operators through painful provisioning flows (cloud backups, OAuth, credential rotation), reports honest health across the LaunchAgent constellation, and keeps every credential in the Keychain.

> What `kubectl` is to Kubernetes — for Sanctum.

## Read first

- [`SPEC.md`](./SPEC.md) — full design specification (mission, doctrine, architecture, CLI surface, config schema, routing, providers, wizards, security, failure modes, roadmap, testing).

## Roadmap, abridged

- **v0.1** — `chat`, `vision`, `cloud setup` (B2 + Drive wizard), `status`, `config validate`. Acceptance: zero-to-backup in ≤ 5 min on a fresh Mac.
- **v0.2** — `doctor`, `backup [run|verify|restore]`, `agent`, `proxy`, Holocron telemetry panel.
- **v0.3** — Storj / S3 / Local NAS in wizard, offline MLX fallback, rate-limit-aware retries, key rotation.
- **v1.0** — Rust dispatcher port, single static binary, Sigstore-signed releases.

## Doctrine alignment

- **Apple+military doctrine** — closed-loop, honest, bounded, defense-in-depth.
- **No hardcoded endpoints** — every IP/port/hostname from `~/.sanctum/instance.yaml` or discovery.
- **Python feature-organic, Rust hardened** — Python prototype, Rust port for hot paths only after the surface stabilizes.
- **Sanctum AI stack** — Claude default, Gemini for spatial/vision, MLX-local as offline fallback. No new Microsoft dependencies.
