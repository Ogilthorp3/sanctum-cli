# Sanctum Ship Bar + Module Spine — Design Spec

- **Date:** 2026-06-01
- **Status:** Draft for review (brainstorm output; awaiting greenlight → `writing-plans`)
- **Author:** Claude Code (session `9cde1ccf`), with the council
- **Sub-project:** #1 of the "Fantastic Four" beta-suite program (see Sequencing)

---

## 1. Context & Motivation

The goal is to make Sanctum **stable enough to ship to a beta user** — someone who
isn't Bert, installing on their own Mac. Sanctum is not one product; it is ~6
independent subsystems (backup, monitoring, Yoda-on-Signal, screen-time, the
cathedral, the bridge). Shipping all of them as separate installers would be four-
plus times the install/secrets/onboarding/docs work and a miserable beta UX.

**Architecture decision (approved):** a *suite on a shared spine*. `sanctum-cli`
becomes the installer + secrets-bootstrap + module-manager; the other powers become
**modules** it provisions, and the existing self-healing watches them uniformly. We
build the foundation and a measurable "ship bar" **once**, then light up modules in
readiness order.

### Why a measurable bar — evidence from this session

On 2026-06-01 a per-job sweep of the haus's launchd jobs (driven by the
`launchd-health-sentinel`) found that **13 jobs flagged "stuck" were not noise — ~6
were real, unattended faults**, none safe to allowlist:

- The Yoda model seat (:1337) returned **6–9 of 10 garbage/empty responses across
  four consecutive nights** (`council-parity-smoke`) — a silent decode-corruption
  regression.
- Two ssh tunnels were **crash-looping live** (17 MB logs each) on a port-squat
  conflict with a staging VM.
- The nightly regression oracle (`kitchen-loop`) had been **dead for 10 nights**
  (wrong Python interpreter under TCC) while double-paging critical.
- The Home Assistant self-healer was **crashing mid-heal**; HA sat at CRITICAL.
- A health probe (`yoda-honor-probe`) reported **all-green in its JSON while the
  monitored path was down ~11 hours** — failures only went to stdout (a false-green).
- The ambient sink itself (chitti) was **dropping the POSTs** during the incident.

This is the precise failure mode the ship bar must prevent: faults indistinguishable
from cosmetic noise, self-healing that crashes, health signals that lie, and silent
rot that no soak ever catches. "Stable enough to ship" has to stop being a vibe and
become a score.

## 2. Goals / Non-Goals

**Goals**
- A declarative **module manifest** that each power ships, describing its services,
  secrets, health probes, alert routing, uninstall, docs, demo, and soak result.
- A **`sanctum doctor --ship <module>`** command that scores a module red/amber/green
  against a concrete, evidence-backed ship bar, with a single verdict + exit code.
- **`sanctum module list/status/install/uninstall`** to manage modules uniformly.
- A **soak harness** that proves a module survives 7 days unattended.
- All of it **additive** — no rewrite of existing `sanctum-cli` code.

**Non-Goals (YAGNI)**
- Designing modules 2–4 here. Each gets its own spec after this lands.
- A remote module registry / marketplace. Modules are built-in or local-path for beta.
- Rust port, PyPI publish, multi-OS. (Out of scope; tracked in SPEC.md v1.0.)
- Fixing the 6 faults found above — they are flagged for triage, not part of this spec.

## 3. Architecture — a thin additive layer on an existing spine

`sanctum-cli` v0.9 already provides most of the spine:

| Spine capability | Status today | File |
| --- | --- | --- |
| brew install / `update` | shipped (v0.8 beta) | external tap `ogilthorp3/sanctum` |
| first-run `onboard` | shipped (splash→recipe→cloud→canary) | `commands/onboard.py` |
| clean `uninstall` (data-preserving, 30-day recoverable) | shipped | `commands/uninstall.py` |
| `self-test` (tier-aware probe registry) | shipped | `commands/self_test.py` |
| generic keychain (no Bert-specific values; default account `sanctum`) | shipped | `keychain.py` |
| strict config (pydantic v2) | shipped | `config.py` |

Three of the six draft bar criteria — clean install/uninstall, beta-owned secrets,
verify-my-install — are therefore **already substantially met**. We add exactly two
new ideas plus their plumbing:

1. **The Module Manifest** — the contract each module declares itself by (§4).
2. **`sanctum doctor --ship`** — the gate that scores a module against the bar (§5–6).

Plus: refactor the flat `PROBES` list in `self_test.py` into a **module-keyed
registry** so each module contributes its probes, and add `sanctum module` (§7).

### Unit boundaries

- `modules/manifest.py` — pydantic models + loader/validator for module manifests.
- `modules/registry.py` — discovers built-in + user manifests; resolves deps; exposes
  probes/secrets/services/uninstall to consumers. One source of truth.
- `commands/module.py` — the `sanctum module …` command surface.
- `commands/ship.py` (or `doctor --ship`) — the bar evaluator. Reuses the `self_test`
  runner loop; adds gate logic.
- `soak/harness.py` — the soak runner + result schema.

Each is independently testable: a manifest validates without a doctor; the doctor
scores against a manifest fixture without live services (then a smoke test runs the
real thing). The manifest is the boundary; consumers never reach past it.

## 4. The Module Manifest — the contract

A module ships one manifest. Built-in modules embed it; user/third-party modules drop
it at `~/.sanctum/modules/<name>.module.yaml`. Validated by pydantic at load.

```yaml
module: monitoring
version: 1.0.0
description: Holocron dashboard + sentinels + R2D2 self-healing
depends_on: []                      # module names that must be installed first

services:                           # launchd jobs this module owns
  - label: com.sanctum.navigator-sidecar
    kind: launchagent               # launchagent | launchdaemon
    keepalive: true
    health_probe: monitoring.sidecar_http   # ref into the probe registry

secrets:                            # keychain entries the module needs
  - account: sanctum
    service: holocron-signing-key
    required: true
    generate: hex64                 # how `module install` bootstraps it if absent
                                    # (hex64 | none — never copies Bert's value)

probes:                             # dotted paths to ProbeResult-returning callables
  - monitoring.sidecar_http
  - monitoring.r2d2_heartbeat

alerts:                             # the alert-hygiene contract
  sink: chitti                      # where ambient health goes; doctor checks it is LIVE
  pager_conditions:                 # the ONLY conditions allowed to reach P0/P1
    - probe: monitoring.sidecar_http
      severity: p1

uninstall:                          # module-scoped teardown; extends global uninstall
  bootout_labels: [com.sanctum.navigator-sidecar]
  revoke_secrets: [holocron-signing-key]
  remove_paths: []                  # data preserved unless --purge
  rename_suffix: ".uninstalled-{date}"

docs: https://sanctum-docs.../monitoring     # resolvable user-facing page
demo: "sanctum module demo monitoring"        # one-command demo, must exit 0
soak:
  min_days: 7
  result_path: ~/.sanctum/soak/monitoring.json   # written by the soak harness
```

Design notes:
- `secrets[].generate` makes secrets-bootstrap **beta-owned by construction** — install
  mints a fresh value or prompts; it never copies an existing Bert value.
- `alerts.pager_conditions` is the allowlist of what may page. Everything else is
  ambient. This is the structural fix for the P0/P1-noise problem.
- `services[].health_probe` ties every KeepAlive service to a probe — no service
  without a liveness check (the gap behind the false-green finding).

## 5. The Ship Bar — concrete gates

`sanctum doctor --ship <module>` evaluates these. Each gate yields green / amber / red.
Module verdict = worst gate. Exit 0 iff no red (amber allowed with `--allow-amber`).

| Gate | Checks | Green | Amber | Red |
| --- | --- | --- | --- | --- |
| **install/uninstall** | manifest has uninstall steps; dry-run them; data-preserve honored | reversible, data preserved | rename-only (no purge path) | no uninstall handler |
| **secrets-bootstrap** | every `required` secret present; none equals a known Bert-default fingerprint | present + non-default | present, default-looking | missing |
| **self-heal** | each keepalive service has a probe + a heal action; the heal action exists and **exits cleanly when invoked dry** | heal proven in soak | heal exists, unproven | no heal, or heal crashes |
| **alert-hygiene** | `alerts.sink` is **reachable right now**; `pager_conditions` are crucial-only; no probe is a false-green | live sink, minimal pager | live sink, broad pager | dead sink OR false-green probe |
| **soak** | `soak.result_path` shows ≥ `min_days` continuous, no unhandled faults | clean ≥7d | partial / <7d | none or failed |
| **docs+demo** | `docs` URL resolves; `demo` command exits 0 | both | one | neither |

Each red maps to a fault we actually observed this session, which is how we know the
gates are the right ones:

- dead sink / false-green → chitti drop + `yoda-honor-probe` lying JSON
- heal crashes → `ha-self-healer` dying mid-heal
- soak catches rot → `kitchen-loop` (10 nights) + `council-parity` (4 nights)

## 6. `sanctum doctor --ship <module>`

Reuses the `self_test.py` runner loop (per-probe timing, real-time rows, summary
panel, `--json`). New logic:

1. Load + validate the module manifest via `registry`.
2. Run the gate checks above (probes come from the module's `probes`).
3. Render a gate table (green/amber/red) + the single verdict.
4. Exit 0 iff shippable; non-zero with the blocking gates named.

`--json` emits a machine verdict for CI. `sanctum doctor --ship all` scores every
installed module. This is "is this fit to hand a stranger," where `self-test` is "is
my install still good."

## 7. `sanctum module` commands

- `sanctum module list [--json]` — installed modules + version + last ship verdict.
- `sanctum module status <name>` — services, secrets present?, probes, soak age.
- `sanctum module install <name|path> [--yes]` — validate manifest, resolve deps,
  bootstrap secrets (`generate`), register services, idempotent.
- `sanctum module uninstall <name> [--purge]` — run the manifest's uninstall steps,
  coordinating with global `uninstall` (shared keychain/labels handled once).
- `sanctum module demo <name>` — run the manifest's `demo` command.

`PROBES` in `self_test.py` becomes `dict[str, list[Probe]]` keyed by module (`cli`,
`haus`, and each installed module); the loader merges them. `--only` filtering and
tier-awareness are preserved.

## 8. The Soak Harness

The proof we keep not having. `sanctum soak <module> [--days 7]`:

- Records a baseline, then on an interval (default hourly) runs the module's probes +
  captures memory pressure level, swap, the module's service exit history, and any
  pager events.
- A run is **clean** only if: zero red probes, zero unhandled faults, no service
  crash-loop, and pressure never went critical *without recovery*.
- Writes `~/.sanctum/soak/<module>.json` (the `soak.result_path`), which the ship bar
  reads. A soak that sees a fault records it — it does not silently pass.

Critically, the soak asserts on **outcomes that the launchd sweep proved get missed**:
a service exiting non-zero on a schedule, a probe that never passes, a heal that loops.
This is the closed-loop version of "it seemed fine."

## 9. Sequencing

1. **Spine + Ship Bar (this spec).** Reference module: **backup** (the CLI's own job;
   smallest gap — proves the mold end-to-end: a beta installs and backs up to *their*
   R2).
2. **Monitoring** — Holocron + sentinels + R2D2. This session's hardening feeds it; the
   alert-hygiene gate is most of its work. *Closing the launchd faults belongs here.*
3. **Screen-time** — standalone repo already; make the Firewalla dependency optional.
4. **Yoda-on-Signal** — most Bert-coupled and most fragile (this session's reboot saga
   + today's 11-hour VM flap prove it); last, once self-heal + secrets-bootstrap are
   battle-proven on the easier three.

## 10. Testing strategy (Contracts at the Boundary)

Per the haus doctrine — structural assertions at layer boundaries are theatre:

- **Manifest contract:** for every built-in manifest, assert each named service has a
  real plist target, each probe path imports, each `revoke_secrets` entry matches a
  declared secret. Derive expectations from the *consumer's* schema, not the author's.
- **Doctor gates:** feed the evaluator a fixture manifest with a deliberately *dead*
  sink and a *false-green* probe; assert it goes red. Test the hostile input, not the
  happy path.
- **Soak:** unit-test the clean/dirty classifier against recorded fault traces from
  this session (kitchen-loop's 10-night streak, council-parity's garbage rows).
- **No mocking cheap boundaries:** `module install`/`uninstall` run against a temp
  `HOME` with real (throwaway) launchd labels + keychain entries, not monkeypatched.

## 11. Build inventory — exists vs. new

**Reuse:** `self_test` runner loop + tier wrapper, `recipes` override pattern (→ module
override), `keychain` read boundary, `config` pydantic base, `uninstall`
preserve/rename/revoke logic, `onboard` orchestration shape.

**New:** `modules/manifest.py`, `modules/registry.py`, `commands/module.py`, the
`doctor --ship` gate logic, `soak/harness.py`, the `PROBES` flat-list → module-keyed
refactor, and `CliConfig.modules`.

## 12. Open questions for Bert

1. **`sanctum-admit` on manoir.** The capacity-doctrine daemon (:2189) is not running
   here — memory says it went live *on the MBP*. Is admission control a per-host ship-
   bar requirement (it would have prevented today's swap saturation), or MBP-only? This
   affects the self-heal gate for the monitoring module.
2. **Spec home.** This lives in `sanctum-cli/docs/specs/`. Keep here, or also surface a
   user-facing page in sanctum-docs once implemented?
3. **Amber policy.** Should beta shipping require all-green, or green-with-documented-
   amber (e.g., soak <7d on a fast-moving module)?
4. **Backup-as-reference scope.** Fold the backup reference manifest into this sub-
   project, or make it the first item of the implementation plan?
