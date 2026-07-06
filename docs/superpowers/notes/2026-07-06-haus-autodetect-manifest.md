# Haus Hardware Auto-Detect — Layer Manifest (2026-07-06)

`sanctum onboard` can now *discover* the haus's network gear and pair each found
device inline, pre-seeded with its discovered IP. Passive-first, fail-open,
honest-verify, zero new dependencies.

## Module map

| Module | Public surface | Role |
| --- | --- | --- |
| `sanctum_cli/gear/types.py` | `Candidate`, `DiscoveredDevice`, `HausInventory` | Pure frozen value types. `Candidate.merge` unions two sightings of one IP (first non-None field wins, hints unioned); `HausInventory.recognized_count` = `len(devices)`. |
| `sanctum_cli/gear/sources.py` | `arp_cache(runner)`, `ssdp(*, search)`, `router_clients(*, lister)` | Passive candidate sources — each **fails open** (a missing binary / socket error / parse miss contributes nothing, never raises). ARP via the `Runner` seam (`arp -a`); SSDP via stdlib `socket` M-SEARCH (no mDNS dep). |
| `sanctum_cli/gear/scan.py` | `discover_haus(net, *, allow_active, sources, fingerprint)`, `build_default_scan(net)`, `Source`, `Fingerprint` | `discover_haus` is pure orchestration over injected seams (unit-tested with fakes): unions passive candidates with the gateway, **always** fingerprints the gateway, fingerprints LAN candidates only under `allow_active` (consent), tallies the rest as unrecognized. `build_default_scan` is the real wiring (ARP+SSDP sources + a registry-backed fingerprint), marked `# pragma: no cover`. |
| `sanctum_cli/devices/sagemcom.py` | `_probe_is_sagemcom(gateway_ip, *, http_post=…)` | Real read-only fingerprint: an unauthenticated SAH JSON-req to a Sagemcom hub returns `XMO_INVALID_SESSION_ERR`. Injected `http_post` seam + httpx default. |
| `sanctum_cli/devices/orbi.py` | `_probe_is_orbi(gateway_ip, *, http_get=…)` | Real read-only fingerprint: NETGEAR Orbi exposes an unauthenticated `currentsetting.htm` with a `Model=RBR/RBS/RBK…` banner. Injected `http_get` seam + httpx default. |
| `sanctum_cli/commands/onboard.py` | `haus-scan` gate (`_run_haus_scan`) + helpers | The onboard front door — see below. |

## The `haus-scan` gate (onboard.py)

- **Registration:** `"haus-scan"` is listed in `RECIPE_GATES["family"]` **immediately
  after `firewalla-compat` and before `network-gear`**, in `_GATE_LABELS`
  (`"Haus hardware scan"`), and dispatched in `_run_gate`. It is present ONLY in the
  `family` recipe (operator/code have no LAN-gear step).
- **Handler `_run_haus_scan(*, yes)`** — `--yes` skips (interactive discovery); else
  consent → discover → for each recognized device, offer inline pairing that REUSES
  the existing primitives `store_device_secret` → `_probe_device` → `set_device_reference`,
  with the **DISCOVERED ip as `host`** (not the gateway). Honest-verify: a device is
  only "paired ✓" after a real read-only auth-probe against its own ip; a rejected
  probe revokes the just-written Keychain secret and persists no `devices` block.
  Fail-open: any discovery exception prints a note and returns `False` — a failed scan
  configures nothing but never crashes onboarding.
- The persisted brand is `dev.brand`, which `build_default_scan`'s fingerprint already
  sets to the **class-level** brand constant (`type(provider).brand`) — the value a
  later `registry.resolve(..., brand_pin=…)` can match (a refined instance brand like
  `orbi-rbr850` would not resolve).

## Seams (what tests inject / patch)

- `sources` + `fingerprint` — `discover_haus`'s injected seams (fakes in `tests/gear/test_scan.py`).
- `http_post` / `http_get` — the fingerprint seams on the two providers.
- `_discover_haus_for_onboard(net, *, allow_active)` — the gate's discovery seam
  (tests inject a ready `HausInventory`; the real active branch is `# pragma: no cover`).
- `_consent_active_scan(yes)` — the one consent prompt (patched to a fixed bool).
- `_provider_for(kind, ip)` — resolves the provider at the discovered ip for the
  auth-probe (`# pragma: no cover` — live registry resolve).
- `_net_context`, `store_device_secret`, `_probe_device`, `_revoke_device_secret`,
  `set_device_reference`, `net_cmd.device_keychain_ref`, `Confirm`, `Prompt` — all
  module-level so `tests/test_onboard_haus_scan.py` replaces every real boundary
  (no live scan / socket / subprocess / Keychain write under pytest).

## Real vs. deferred

**Live now**
- ARP-cache and SSDP passive sources (dep-free).
- Registry-backed fingerprint over the gateway + consent-gated LAN candidates.
- Real Sagemcom + Orbi fingerprints (filling the former stubs).
- Inline pairing at the discovered ip, reusing the network-gear pairing primitives.

**Deferred (seams left in place, not wired)**
- `router_clients(*, lister)` is a real **`[]`-returning seam** — no paired provider
  exposes a DHCP/client table yet, so `lister` is `None` in the MVP (YAGNI, not a TODO).
- EcoFlow / Tuya discovery, auto-configure (write settings, not just pair), and an
  "unknown-roster" review UI are out of scope.
- **Arc wiring — DONE (live):** `haus-scan` is in `_CHAPTER_GATES["Your Network"]`
  (right after `firewalla-compat`, before `network-gear`), so the narrated onboarding
  arc actually runs it. The six full-arc test helpers that reach the chapter mock
  `_run_haus_scan` — the same pattern `network-gear`/`ha-green` use, because it prompts
  for scan-consent + does real ARP/SSDP/httpx and must not do so under test.
  `make check` green: 1499 passed.

## The ONE attended check (spec open item #2)

Run `sanctum onboard` on Bert's real haus and confirm the scan finds the **Bell hub**
(Sagemcom F5697) and the **Orbi** at their real IPs and pairs each with a real admin
password. The fingerprint **shape** is locked (injected `http_post`/`http_get` seam +
marker match); if the live markers differ, adjust the constants **and their fixtures
together**:
- Sagemcom: `_SAH_LOGIN_URL` path (`/cgi/json-req`) + `_SAGEMCOM_MARKER`
  (`XMO_INVALID_SESSION_ERR`) in `devices/sagemcom.py`.
- Orbi: `_ORBI_URL` (`/currentsetting.htm`) + `_ORBI_MARKERS` (`Model=RBR/RBS/RBK`) in
  `devices/orbi.py`.

A wrong constant is a one-line + fixture change, never a redesign — and because the scan
is **fail-open**, a wrong marker just means the arc reports "no configurable gear found"
and continues (never a crash, never a false pairing). The gate is already wired into the
live arc, so this attended run is the only thing between "built" and "field-confirmed".
