# Sanctum Net Heal — Topology-Adaptive Self-Healing Node — Design

> **Status:** approved (2026-07-02), pre-implementation. **Branch:** `feat/net-heal` (off `main` 35abcfd).
> **Owner-facing name:** `sanctum net heal` — the layer that keeps a Sanctum node online across any topology.

## Problem (the doctrine violation)

A Sanctum hub must stay reachable and serving across **any** network topology change — single-NAT,
double-NAT fallback, hub swap, subnet renumber, roaming (Bell `192.168.2.x`, Orbi/FW `10.0.0.x`, an
iPhone hotspot `172.20.10.x`). It didn't. Root cause (found via Bert's controlled experiment, both Mini
and MBP on Bell): the **Mini was pinned to a STATIC IP `10.0.0.10` / gw `10.0.0.1`** during the
single-NAT renumber. A static config can't adapt — on Bell it's the wrong subnet (the Mini reaches the
Bell gateway `192.168.2.1` at **0% loss** — the RF was always fine — but its configured gateway
`10.0.0.1` is 100% loss), and on the FW LAN a static IP under a rotating MAC produced the ARP-corruption
and quarantine that consumed days of misdiagnosis. The **MacBook Pro is DHCP and simply works** on every
network. Overnight, a triple reboot knocked out the single-NAT DMZ and the haus **fell back to double-NAT**
(overlapping `10.0.0.x` on both the FW LAN and the Sagemcom WAN) — and nothing on the node adapted.

**A brittle client-side static pin is the anti-pattern.** The node must be topology-adaptive and
self-healing, apple-like (declarative, verified, invisible) and military-grade (fail-safe, never strands
itself, works across every topology).

## Goal

`sanctum net heal` — a node keeps itself on a **correct, working L3 identity for whatever network it is
on**, self-heals the safe failure modes automatically, and can **never be stranded** (the tailnet + a
wired bridge remain the always-on spine). Ship it for the Mini and as the reusable Sanctum-node pattern
for beta.

**Non-goals:** driving the router/NAT side (the haus single-NAT/double-NAT cutover is a separate,
supervised domain — this node layer must work *regardless* of which the haus is running); fixing RF (the
RF was never the problem); non-macOS nodes.

## Decisions locked (brainstorming)

1. **Self-heal autonomy = auto-heal-safe + tailnet-safety-net + stop-and-alert-on-risky.** The node
   auto-remediates the *safe, reversible* cases itself; keeps the tailnet + TB5 spine alive as the
   never-strand invariant; and *stops + alerts* (never loops) on risky/ambiguous cases or a heal that
   didn't restore in N attempts.
2. **Scope:** Mini-first, productized as the reusable Sanctum-node pattern (beta).

## Doctrine this encodes (the fixes for what the static pin violated)

1. **DHCP, never a client static pin.** IP stability comes from a *router-side DHCP reservation* on the
   node's stable hardware MAC — never a `Manual`/static config on the client. The healer treats a
   drifted-to-static config as a fault to remediate.
2. **Address by stable NAME, not IP.** Inter-service wiring uses tailnet MagicDNS / the sanctum-endpoints
   resolver, so an IP change (topology switch) never breaks it. The healer *flags* Sanctum config wired to
   a hardcoded LAN IP.
3. **The tailnet is the always-on spine.** The node stays reachable/serving over Tailscale (+ the TB5
   wired bridge) regardless of LAN topology; that is the never-strand fallback the healer guarantees before
   attempting any LAN mutation.

## Architecture

Additive extension of the existing **pure-core + thin-shell** `sanctum net` package
(`sanctum_cli/net/` — `detect.py`/`types.py`/`playbooks.py`/`verify.py`/`system.py`, injected
`CommandRunner`) and `sanctum_cli/commands/net.py` (Typer). Reuses the `sanctum link` `IdentityProbe`
(gateway/ARP reads) and the trust-guardian pattern. New logic is additive; existing `net`/`link` tests
keep passing.

### New pure core (`sanctum_cli/net/heal.py`)

**1. `NetPosture` dataclass** — the node's live L3 posture, from `IdentityProbe` + a thin new read:
`iface`, `config_method` (`DHCP`/`Manual`/`LinkLocal`), `ip`, `subnet`, `gateway`, `gateway_reachable`,
`associated`, `default_route_iface`, `on_tailnet` (tailnet IPv4 present), `tb5_up`.

**2. `diagnose_posture(posture) -> PostureDiagnosis`** — pure truth table → verdict + a *proposed heal
action* (data, not execution):

| Verdict | Condition | Proposed heal (safe?) |
|---|---|---|
| `HEALTHY` | DHCP + gateway_reachable | none |
| `STATIC_DRIFT` | `config_method == Manual` | `flip_dhcp` — **safe/auto** |
| `GATEWAY_DEAD` | associated + `gateway_reachable is False` + DHCP | `dhcp_renew` — **safe/auto** |
| `WRONG_SUBNET` | gateway not in the node's subnet, or ARP mismatch | `dhcp_renew` — **safe/auto** |
| `DOUBLE_NAT_OVERLAP` | node's subnet observed on two sides / overlapping | `alert_only` — **risky, NO auto** |
| `UNVERIFIED` | can't read | none (fail-closed) |

Each action carries `safe: bool`. The healer only executes `safe` actions; `alert_only`/UNVERIFIED →
stop + alert.

**3. `plan_heal(diagnosis, attempts, tailnet_ok) -> HealPlan`** — pure guard layer: returns the concrete
action ONLY IF `diagnosis.action.safe` AND `attempts < MAX_HEAL_ATTEMPTS` (=3) AND `tailnet_ok` (the
spine is alive — never mutate the LAN if the tailnet fallback is down, unless a wired bridge is up).
Otherwise returns a `stop_and_alert` plan with the reason. **Fail-safe + never-strand encoded here.**

### Thin shell (`sanctum_cli/commands/net.py` — extend)

- **`sanctum net heal`** (default: **dry-run** — diagnose + print posture + the *would-do* action, no
  mutation). `--apply`: execute the safe heal (needs root — see daemon) with **snapshot → act →
  verify-back → auto-revert-on-fail**: snapshot the current IPv4 config, run `networksetup -setdhcp`
  / `ipconfig set en1 DHCP`, wait for a lease + gateway reachability, and if it doesn't come up, revert
  to the snapshot and stop+alert. Honest-verify: "healed" only from a real re-probe (got a lease AND
  gateway_reachable).
- **`sanctum net status`** already shows topology; add the posture verdict + tailnet-spine state.

### Self-healing daemon (`com.sanctum.net-heal`, sudo-gated LaunchDaemon)

- Installed by `sanctum net heal --install` (writes the plist + a wrapper; **the one sudo action** — a
  LaunchDaemon so it can `setdhcp`/renew). Runs every ~120 s: `diagnose_posture → plan_heal → (execute
  safe | stop+alert)`, writes a heartbeat, and **verifies the tailnet/TB5 spine each cycle** (alerts if
  the spine is down — the "node is about to be strandable" page). MAX_HEAL_ATTEMPTS backoff: after 3
  failed heals of the same fault, stop auto-healing + alert (no loop — the mini-wifi-reattach toggle-storm
  lesson). Alerts via Force Flow / the vault (best-effort, degrade gracefully).

### Onboard integration

A **"network resilience" gate** in `sanctum onboard` (Your Network chapter): assert DHCP-not-static,
confirm a router-side reservation exists (guidance if not), install the heal daemon, verify the tailnet
spine — narrated, green-check verified, skippable.

## Data flow

```
IdentityProbe + config-method/route/tailnet reads → NetPosture ─→ diagnose_posture ─┐
                                                                                     ▼
attempts (persisted) + tailnet_ok ───────────────────────────→ plan_heal ─→ HealPlan
                                                                                     ▼
                              safe → snapshot → setdhcp/renew → verify-back → (ok | revert+alert)
                              risky/UNVERIFIED/spine-down → stop + alert (never mutate)
```

## Error handling, safety & fail-safe (military-grade)

- **Never-strand:** no LAN mutation unless the tailnet (or TB5) spine is confirmed alive first; a heal is
  snapshot→act→verify→auto-revert; the daemon can be killed via a `DISABLED` sentinel.
- **No-loop:** MAX_HEAL_ATTEMPTS=3 per fault, then stop + alert.
- **Fail-closed:** UNVERIFIED / can't-read → no action.
- **Honest-verify:** "healed/adaptive/online" only from a real re-probe (lease + gateway reachable).
- **Stays in its lane:** never touches the router/NAT (double-NAT overlap → alert only, that's the
  supervised haus domain).

## Testing (TDD; gate = `make check` → ruff + mypy --strict + pytest, run via the worktree venv)

- Pure truth-tables for `diagnose_posture` (HEALTHY/STATIC_DRIFT/GATEWAY_DEAD/WRONG_SUBNET/
  DOUBLE_NAT_OVERLAP/UNVERIFIED), built from the **literal** captured states: the Mini's Manual/10.0.0.10
  on Bell (STATIC_DRIFT + WRONG_SUBNET), the double-NAT overlap snapshot, a healthy DHCP node.
- `plan_heal` guards: risky→stop, attempts≥3→stop, tailnet-down→stop, safe+ok→act.
- Injected-runner posture probe (Manual-static fixture; DHCP fixture; iface-absent→UNVERIFIED).
- CLI: `net heal` dry-run prints the would-do; `--apply` on a fixture snapshots+verifies+reverts on a
  simulated failure (no live `networksetup` in tests — inject the runner).
- Onboard gate: runs, skippable, `--yes`, mocked — **no live network/sudo calls in tests**.

## Distribution

Stack on `main`; TDD build (subagent-driven) → jedi-council whole-branch review → merge → bump beta.
The **immediate Mini un-break** (flip static→DHCP + Private-Address-Off) is delivered as the first live
run of `sanctum net heal --apply` / the daemon install — the one sudo action, which both fixes the Mini
now and installs the durable self-heal.
