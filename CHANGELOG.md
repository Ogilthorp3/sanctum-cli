# Changelog

## 0.15.4 — 2026-08-07

### Added
- **`sanctum service-user`** — greenfield hive service principal install (user `sanctum`,
  wave-1 LaunchDaemons for proxyd / force-flow / memory-vault). Packaged plists;
  no pre-synced sanctum-config required. Operator onboard gate + self-test + doctor.

All notable changes to `sanctum-cli` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.15.3] - 2026-07-24

### Added

- `sanctum upgrade` registry now tracks the manoir's `mlx-finetune/.venv` and `yoda-voice-agent/.tts-venv` (mlx-lm as the drift canary), so the daily sentinel catches MLX drift in the training and voice stacks instead of only the satellite brain venv.

## [0.15.2] - 2026-07-24

### Removed

- `gemini-cli` dropped from the `sanctum upgrade` registry — retired from the haus (Bert, 2026-07-24); the binary was uninstalled from all machines.

## [0.15.1] - 2026-07-20

### Fixed

- `sanctum upgrade` now invokes a venv's pip as `<venv>/bin/python -m pip` instead of the `bin/pip` console script, whose shebang bakes in the venv's original path — a relocated/renamed venv (e.g. the chalet mlx venv) has a working python but a broken pip binary, which made upgrade report the tool absent. Live-fire-verified on the chalet.

## [0.15.0] - 2026-07-19

### Added

- `sanctum upgrade` — toolchain currency in one command. Inventories every
  tool sanctum rides on (brew formulae, npm globals, pip venvs) against a
  curated registry, resolves the latest *stable* version per manager (brew
  bottles, npm `latest` dist-tag, pip final releases — never beta), and
  prints an old→new plan table tagged by what each tool makes sanctum
  (smarter/safer/faster). Check mode is read-only and exits 1 when upgrades
  exist (sentinel/cron-friendly); `--apply` upgrades one tool at a time,
  re-probes the installed version (honest-verify), runs per-tool
  post-checks, prints restart hints for daemons riding on what changed, and
  finishes with the `sanctum self-test` gate. Respects `brew pin` as HOLD;
  npm install-aliases (`denchclaw@npm:openclaw`) are recognized and upgraded
  through the alias. `--only`, `--json`, `--skip-self-test` supported.

## [0.14.1] - 2026-07-03

Pre-beta clean-install pass — removes operator-specific state that shipped as
defaults (or into a fresh operator's config) and stops onboarding crashing
without `restic`.

### Fixed

- `sanctum onboard` no longer hard-fails (`EXIT=1`) on a machine without
  `restic`: the "Your Data" backup chapter now skips gracefully. Only the
  missing-`restic` case is swallowed; every other setup error still surfaces.

### Changed

- HA Green defaults are generic, not one operator's LAN (host -> `homeassistant.local`,
  override `HA_GREEN_URL`); the Tailscale suffix resolves from local `tailscale status`
  (never hardcoded); the HA device MAC comes from `HA_GREEN_MAC` (default empty, no
  longer written into another operator's `instance.yaml`); OOB bridge subnet via
  `SANCTUM_OOB_PREFIX`.


## [0.10.2] - 2026-06-19

A follow-up personal-infra sweep that caught functional defaults and
user-facing strings the v0.10.1 pass missed.

### Changed

- Routed the Firewalla SSH key path through instance.yaml
  (`firewalla.ssh_key`); genericized remaining personal host names in
  user-facing recipe/ship strings.

## [0.10.1] - 2026-06-19

A beta-portability and honesty pass: no shipped code points at the author's
personal infrastructure anymore, and the SPEC's security model now describes
what the CLI actually does rather than what it aspires to.

### Changed

- **Removed hardcoded personal defaults.** The GitHub owner, deadman heartbeat
  repo, and bridge URL no longer carry a baked-in personal account/host. They
  now resolve from `~/.sanctum/instance.yaml` (`vcs.github_owner`,
  `vcs.deadman_repo`, `secrets.cloudflare_bridge_domain`) or — for GitHub — the
  authenticated `gh` user (`gh api user`); when nothing resolves they raise a
  clear `UserError` pointing at the right config key instead of silently
  addressing someone else's infrastructure. The generated host-backup README
  now shows the *resolved* owner in its clone command.
- **Genericized the council Yoda persona** — the operator name and host
  nickname are read from `instance.yaml` (`notifications.owner_name`,
  `instance.name`) and fall back to generic phrasing ("the operator", "your Mac
  Mini") instead of baked-in literals.
- **Corrected SPEC security-model overclaims.** `sanctum update` is documented
  as a Homebrew-tap upgrade gated by a post-upgrade self-test (Sigstore/cosign
  signing is a v1.0 roadmap item, not implemented); the install-path /
  world-writable-dir self-check is moved to roadmap (not implemented); and
  provider-transport security is described accurately (mTLS/CA-pinning on the
  local proxy, standard certificate-validated HTTPS to Anthropic/Google, no
  public-endpoint pinning).
- **Docs accuracy** — the cloud-setup wizards are described as `rich.prompt`
  guided flows (Textual was the original design, never adopted); the README
  install is now the single-line `brew install ogilthorp3/sanctum/sanctum-cli`
  (auto-taps) matching every other doc.

## [0.10.0] - 2026-06-19

First public beta. The headline is honest packaging: one license, one version, a
real end-user install path, and a clean line between the beta-safe command set and
the commands that need the author's full Sanctum haus.

### Added

- **`sanctum net optimize`** — single-NAT / DMZ topology wizard that detects
  double-NAT behind an ISP gateway and walks the operator through fixing it
  (Bell field learnings: the `/1` route trap, 1492 PPPoE MTU, PPPoE alternative).
- **`sanctum net speedtest`** — an honest throughput doctor with fail-soft truth
  reporting rather than optimistic numbers.
- **`sanctum init`** — scaffolds a minimal, valid `~/.sanctum/instance.yaml` so a
  fresh Mac can run the CLI without hand-writing YAML; `sanctum onboard` now
  bootstraps the config on first run automatically.
- **Endocrine system** (the seventh organ) — a read-only council disposition /
  creativity regulator; `sanctum endocrine` is its control surface. Off by
  default and byte-identical to before until a gland is running.
- **Continuous integration** — GitHub Actions runs ruff + mypy + pytest on every
  push and pull request.
- **Haus-only command banner** — commands that require the full Sanctum haus
  (`brainstorm`, `council`, `chat`, `code`, `bridge`, `proxy`, `agent`,
  `screen-time`, `devices`, `schedule`, `endocrine`) now detect missing infra
  cheaply and print a clean "needs a full Sanctum haus" banner, exiting without a
  traceback. Commands an operator with the haus runs are unaffected.
- `SECURITY.md` (vulnerability-disclosure policy) and this `CHANGELOG.md`.

### Changed

- **License reconciled to FSL-1.1-MIT** — the Functional Source License 1.1 with
  an **MIT** future grant (converts to MIT two years after release). The
  `LICENSE` file, `pyproject.toml`, and the README now agree; the previous
  `Proprietary` / Apache-future-grant mismatch is gone.
- **Version single-sourced and bumped to 0.10.0** — `__version__` derives from the
  installed package metadata, so `sanctum --version` always tracks `pyproject.toml`.
- **README rewritten** — adds an end-user **Install** section (`brew install` and
  the `curl … | sh` one-liner) above the development flow, and the **Status**
  section now reflects the shipped command set instead of the old bootstrap claim.
- Council + self-test transport moved to CA-pinned TLS (`https`) against proxyd.
- restic / onboard hardening: hermetic cloud-setup precheck, `0600` state files,
  fan-out truth and fail-soft honesty across the CLI.

### Fixed

- self-test coder-cathedral probe repointed `:1338` → `:3301` (the retired
  coder-14b stale endpoint).

[Unreleased]: https://github.com/ogilthorp3/sanctum-cli/compare/v0.10.1...HEAD
[0.10.1]: https://github.com/ogilthorp3/sanctum-cli/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/ogilthorp3/sanctum-cli/releases/tag/v0.10.0
