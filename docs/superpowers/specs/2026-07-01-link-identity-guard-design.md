# Link Identity Guard — Design

> **Status:** approved design (2026-07-01), pre-implementation.
> **Feature branch:** `feat/link-identity-guard` (stacked on `feat/link-optimizer`).
> **Owner-facing name:** the anti-quarantine / stable-Wi-Fi-identity layer inside `sanctum link`.

## Problem

A Sanctum node on Wi-Fi can be **fully associated at the radio layer yet unreachable on the
LAN** — 100% loss to its own gateway while a trickle of NAT'd internet survives. Root cause
(diagnosed live on the Mini `manoir`, 2026-07-01): macOS re-defaults **"Private Wi-Fi Address"
to Rotating** whenever a network is re-joined (after a router swap, subnet renumber, or reboot).
The node then presents a *locally-administered, changing* MAC instead of its burned-in hardware
MAC. Any router that keys trust/identity to a MAC — a DHCP **reservation**, a device
**allow-list**, or a Firewalla **quarantine tag** — no longer recognizes the node, so it is
isolated. The failure is invisible to radio diagnostics (RSSI/SNR/BSSID all look perfect) and
recurs on every re-join, which is why it kept coming back.

The fix is to make fixed-infra nodes present a **stable, hardware MAC on the home SSID**,
**enforced** so macOS cannot silently re-randomize it, **detected** when it drifts, and
**self-healed**. This must be automatic for beta users, apple-like (guided + verified, never
blind), and military-grade (fail-closed, defense-in-depth, self-healing).

### Why this is not an RF problem (the trap to avoid)

The differential is decisive: a second Mac in the same room, on the same mesh node (byte-identical
BSSID), same chipset/firmware/OS, was rock-solid. Radio, mesh, and location were never at fault.
Diagnostics that stop at "signal is good / device is associated" **miss this class entirely**. The
Identity Guard's job is to look one layer up — at *who the node is on the network*, not *whether the
radio works*.

## Goal

Every beta user's **fixed-infra** Macs automatically present a stable, router-trusted Wi-Fi
identity on their home network — **detected → enforced (guided one-click) → self-healed** — so
macOS MAC-rotation can never isolate a node behind a DHCP reservation or device-trust again.

**Non-goals:** MDM enrollment (rejected — too heavy/invasive for a beta); disabling MAC
randomization globally or off-home (privacy regression); auto-enrolling roaming laptops (privacy);
fixing non-macOS nodes (out of scope — the bug is macOS-specific).

## Decisions locked (from brainstorming)

1. **Automation model:** guided one-click + self-heal, **no MDM**, **router-agnostic** (the fix is
   client-side; it helps behind *any* router). macOS requires the user to approve a Wi-Fi profile
   once — we lean into that as the single, apple-like consent moment.
2. **Node targeting:** **auto-classify per node.** A SERVER-class node (always-on, static/reserved
   IP, rarely roams) is enrolled automatically on the home SSID; a ROAMER-class node (laptop, many
   SSIDs) keeps its private/rotating MAC and only gets an *opt-in nudge*. `UNKNOWN` is treated as
   ROAMER (privacy-first). The user can always override.

## Architecture

Extends the existing **pure-core + thin-shell** pattern already established in
`sanctum_cli/net/link.py` (pure, testable, injected `CommandRunner` seam) and
`sanctum_cli/commands/link.py` (Typer shell). All new logic is additive; nothing in the P1/P2 link
feature changes behavior.

Reused as-is (already on `feat/link-optimizer`):
- `render_mac_stability_profile(ssid, hardware_mac, org="Sanctum", encryption_type="WPA3") -> str`
  — emits the per-SSID `.mobileconfig` (`com.apple.wifi.managed`, `MACAddressRandomization: False`),
  deterministic `uuid5`. **This is the enforcement artifact.**
- `analyze_mac(current, hardware) -> MacAudit`, `is_locally_administered(mac) -> bool`,
  `probe_wifi(run) -> WifiProbe`.
- The sentinel (`SENTINEL_SCRIPT`, `render_plist`, `com.sanctum.wifi-stability`, 180 s) and the
  `Diagnosis` framework (`RADIO/LOAD/SCAN/HEALTHY/NO_DATA`).

### New pure functions (`sanctum_cli/net/link.py`)

**1. `IdentityProbe` dataclass** — the live network *identity* facts (distinct from the link-health
sentinel log). Read from `ipconfig getsummary <iface>` (+ one bounded gateway ping), never a scan:

```python
@dataclass(frozen=True)
class IdentityProbe:
    iface: str                        # "" when Wi-Fi iface not found (UNVERIFIED)
    ssid: str | None
    current_mac: str                  # live association MAC
    hardware_mac: str                 # burned-in
    associated: bool                  # LinkStatusActive == TRUE
    router_arp_verified: bool | None  # RouterARPVerified (None if field absent)
    gateway_reachable: bool | None    # bounded ping of the default gw (None if not attempted)
```

**2. `diagnose_identity(probe: IdentityProbe) -> IdentityDiagnosis`** — pure. Verdict truth table
(built from the *actual* `ipconfig getsummary` output captured on the Mini):

| Verdict | Condition | Meaning |
|---|---|---|
| `IDENTITY_QUARANTINED` | `associated` AND (`router_arp_verified is False` OR `gateway_reachable is False`) AND `is_locally_administered(current_mac)` | The tonight signature — associated but LAN-dead on a rotating MAC. |
| `IDENTITY_ROTATING` | `current_mac != hardware_mac` AND reachable | At-risk: randomized MAC, works now, will break on the next trust-reset. |
| `IDENTITY_STABLE` | `current_mac == hardware_mac` AND reachable | Correct — hardware MAC, reachable. |
| `IDENTITY_UNVERIFIED` | `iface == ""` OR `not associated` OR fields unreadable | Cannot tell → **do nothing**. |

`IdentityDiagnosis` carries `verdict`, human `detail`, `remedy`, and the probe. This is
**router-agnostic** — every input is read from the Mac itself.

**3. `classify_node(signals: NodeSignals) -> NodeClass`** — pure, returns `SERVER | ROAMER |
UNKNOWN` with a `reason`. `NodeSignals` (gathered by a thin probe): `uptime_days`,
`ip_config_method` (`Manual`/`DHCP`), `ip_is_reserved_or_static: bool`, `distinct_ssids_seen: int`,
`is_portable: bool` (hw.model contains "Book"). Heuristic, conservative:
- `SERVER` when not portable AND (long uptime OR static/reserved IP) AND few SSIDs.
- `ROAMER` when portable OR many distinct SSIDs.
- `UNKNOWN` otherwise → **treated as ROAMER** downstream.

**4. `firewalla_quarantine_check(mac, transport) -> QuarantineFinding | None`** — *optional
enrichment*, only invoked when a Firewalla + creds are already available via the existing netgear
seam. Returns whether the node's MAC sits in a Firewalla quarantine tag (the tag-18/DAP signal).
Injected transport (no hard dependency); returns `None` when no Firewalla is present. **Never
required and never blocks** the router-agnostic path.

### Thin shell (`sanctum_cli/commands/link.py`)

- **`sanctum link status`** — additionally renders the current **IDENTITY** verdict beside the
  existing link-health verdict (two independent reads: identity = now; health = sentinel log).
- **`sanctum link optimize`**:
  - *default (read-only):* `probe_identity → diagnose_identity → classify_node`, print verdict +
    node class + recommendation. No writes.
  - *`--apply`:* if `SERVER` AND verdict in {`IDENTITY_QUARANTINED`, `IDENTITY_ROTATING`} →
    `render_mac_stability_profile(home_ssid, hardware_mac, enc)`, write the `.mobileconfig`, print
    the narrated **one-click approve** steps, and drop a verify-marker. If `ROAMER`/`UNKNOWN` →
    print the **opt-in nudge** and do nothing unless `--force`. `enc` is **detected** from the
    probe's `Security` field (`ipconfig getsummary` → WPA2/WPA3/…) and passed through — a mismatch
    would make macOS create a non-joining duplicate network, so the detected value (safe default
    `WPA2`, which covers WPA2/WPA3-personal transition) is a hard requirement, not a guess.
  - *`--verify`:* re-probe; assert `current_mac == hardware_mac` on the home SSID AND
    `router_arp_verified is True`. Prints ✓ only on a real post-fix read (honest-verify).
- **Sentinel extension:** each cycle also logs the identity tuple as an
  `id=cur=…,hw=…,arp=…` token. When the sampler itself detects the drift signature
  (rotating MAC + `arp=FALSE`) it stamps a `,drift=1` field **inside** that token — kept
  inside the token, not as a trailing word, so the line's trailing health flag stays
  last and both invariants hold (`_LINE` still matches up to `load=[<num>`, and
  `parse_log`'s `endswith("DEGRADED")` still reads the health flag). The pure detectors
  `parse_identity` + `identity_is_drift` consume the tuple (and honor the stamped
  `,drift=1`); the enforced profile prevents re-randomization; the sentinel is the
  safety-net that catches a *missing or removed* profile and a genuine re-drift.

  > **Deferred (implemented as producer + detectors; log-tuple consumer is future):** the
  > sentinel now *produces* the `,drift=1` marker and the pure `parse_identity`/`identity_is_drift`
  > detectors *consume* it (tested against the real bash producer), but `status` does not yet read the
  > logged identity tuple back to re-nudge. The **live `probe_identity` path in `status`/`optimize`
  > is the current drift surface** (it reads identity now and renders `IDENTITY_QUARANTINED`/
  > `IDENTITY_ROTATING`); the logged-tuple/`,drift=1` re-nudge from the on-disk log — including any
  > `DEGRADED-IDENTITY`-style status wiring and desktop notification — is future work. The detectors
  > are wired to a real producer, not silently orphaned.

### Onboarding integration (`sanctum_cli/commands/onboard.py`)

Add a **"Wi-Fi identity" gate** to the existing **"Your Network"** chapter (chapter/gate
conventions already established; each gate is skippable and keyed by recipe + chapter):
`probe_identity → classify_node` → if `SERVER` and rotating/quarantined, generate the profile and
walk the approve **inline**, narrated, with a **green-check** verify line
(`RouterARPVerified:TRUE` + MAC==hardware — derived from a real outcome, per honest-verify
doctrine), then `link install` the sentinel so it is watched thereafter. Skippable, idempotent,
fail-closed. A ROAMER node gets a one-line informational nudge and is left alone.

## Data flow

```
ipconfig getsummary <iface>  ─┐
networksetup -getmacaddress ──┤
bounded gateway ping         ─┤→ IdentityProbe ─→ diagnose_identity ─┐
uptime / IP-config / SSIDs  ──┴→ NodeSignals    ─→ classify_node    ─┤
                                                                     ▼
                              (SERVER ∧ {QUARANTINED,ROTATING})  render_mac_stability_profile(home_ssid, hw_mac, enc)
                                                                     ▼
                                    write .mobileconfig ─→ guided one-click approve (user) ─→ --verify re-probe
                                                                     ▼
                              sentinel logs identity every 180 s ─→ status surfaces drift ─→ self-heal nudge
```

Firewalla enrichment (when present) tees off `IdentityProbe.current_mac` →
`firewalla_quarantine_check` → adds "confirmed in FW quarantine tag" to the detail; optional.

## Error handling, safety & privacy (military-grade)

- **Fail-closed:** `IDENTITY_UNVERIFIED` → no profile, no claim, ever. A profile is only generated
  on a *positively-diagnosed* SERVER node.
- **Honest-verify:** "fixed / protected / stable" is only ever printed from a real re-probe showing
  MAC==hardware AND `RouterARPVerified:TRUE` — never because "the step ran."
- **Privacy (defense against our own fix):** the profile is **per-SSID (home only)**; roaming to
  any other network still randomizes; ROAMER/UNKNOWN nodes are never auto-enrolled; global
  randomization is never touched.
- **Consent:** `--apply` gates every write; the onboard gate is skippable; macOS enforces the
  manual profile approval regardless — that *is* the consent moment.
- **Secrets:** the profile is built locally; the PSK is read from the node's own
  keychain/secret and **never logged** (redacted in all output; a hostile-value test guards the
  boundary).
- **Idempotent:** deterministic `uuid5` (already) → no duplicate profiles; a marker file records
  "generated/approved" so re-runs are no-ops.
- **Router-agnostic core; Firewalla enrichment strictly optional** — a beta user on any router
  gets the full client-side fix; Firewalla users get bonus confirmation.

## Testing (TDD; gate = `make check` — ruff + mypy --strict + pytest)

- **Pure truth-tables:** `diagnose_identity` for all four verdicts, the `IDENTITY_QUARANTINED`
  case built from the *literal* `ipconfig getsummary` output captured on the Mini
  (`RouterARPVerified: FALSE` + LAA MAC). `classify_node` SERVER/ROAMER/UNKNOWN boundaries
  (portable, uptime, static-IP, SSID-count).
- **Probe (injected runner):** a quarantined-server fixture → `IDENTITY_QUARANTINED`; a
  healthy-hardware-MAC fixture → `IDENTITY_STABLE`; iface-absent → `IDENTITY_UNVERIFIED`
  (no silent fallback).
- **CLI:** `optimize` on a SERVER+quarantined fixture writes a valid `.mobileconfig`
  (round-tripped via `plistlib`) + prints guided approve; on a ROAMER fixture nudges only (no
  write); `--verify` prints ✓ only when the re-probe passes.
- **Boundary/hostile:** profile render with a PSK containing `%`, a space, and a non-ASCII char —
  proves the escaping (`plistlib` handles it) and that the PSK never leaks to logs.
- **Onboard gate:** runs, is skippable, `--yes` path, mocks keychain/profile/probe — **no live
  API/`profiles`/Wi-Fi calls in tests**.
- **Sentinel:** identity-line format parses; drift (hardware→rotating) detected and classified
  `DEGRADED-IDENTITY`, not `RADIO`.

## Distribution

Stack on `feat/link-optimizer`; council-review (jedi panel) the whole branch; merge to `main`; bump
the sanctum-cli beta so beta users receive it through `sanctum onboard` (auto-setup at enrollment)
and `sanctum link` (on-demand + ongoing self-heal). Cross-link the DHCP-reservation-vs-rotating-MAC
trap into the single-NAT/router-swap runbook so a future router swap re-trusts nodes' hardware MACs
as part of cutover.

## Open questions

None blocking. Future (out of scope here): a `firewalla trust-node <hw-mac>` primitive to pin a
Sanctum host to a trusted tag + monitoring-exemption (the router-side complement, Firewalla-only);
extending the Guard to iOS/other platforms if Sanctum nodes ever run there.

Future integration point — `sanctum link rescue`: when the Wi-Fi identity is UNVERIFIED or the sole
radio link is down, the sibling TB5 Layer-3 bridge (MBP↔Mini over the Thunderbolt bridge, `10.0.5.x`)
is a router-independent path to re-probe and re-assert identity. The Guard's `probe_identity` and
`classify_node` are already router-agnostic (L3 read), so `link rescue` can reuse them over the TB5
transport to recover a node whose Wi-Fi association is flapping. Not built here.

Known gap carried into whole-branch review (Task 9 verification): Task 6 (`sanctum link optimize`
gaining `--verify` for an honest re-probe ✓/✗ and a SERVER-gated `--apply`) was not landed in this
run — the commit history goes T5 → T7. Today `optimize` is a read-only audit plus an ungated
`--apply` that only renders the `.mobileconfig` (never mutates the radio, smoke-verified), so behavior
is safe and additive; but the `--verify` honest-re-probe and node-class gating on `--apply` remain to
be implemented per the plan's Task 6 before that surface is complete. The `link status` IDENTITY
verdict (T5) already provides the honest re-probe read in the interim.
