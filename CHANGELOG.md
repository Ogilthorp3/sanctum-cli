# Changelog

All notable changes to `sanctum-cli` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ogilthorp3/sanctum-cli/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/ogilthorp3/sanctum-cli/releases/tag/v0.10.0
