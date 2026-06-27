# Single-NAT cutover — code handoff (2026-06-27, worktree /tmp/sanctum-cli-flip)

> ## ⬆ UPDATE 2026-06-27 (later) — FIX-b LANDED + off-LAN gate leg closed. The lines below this banner that say "FIX-b deferred" are STALE; this banner supersedes them.
>
> **FIX-b is LANDED** (`c35eb4f`) and the keystone OOB-gate Mini leg is now config-driven too
> (this session, committed on this branch). The box + Mini transport is **config-first**: the armor
> deploy, the `observe_lease`/`verify` box reads, the `_DmzRollbackProvider` recovery re-lease, AND
> the OOB recovery gate (both its box leg and now its Mini leg) all read `devices.firewalla.host` /
> `devices.firewalla.ssh_user` / `devices.mini.host` from `~/.sanctum/instance.yaml` at call time,
> defaulting to the LAN coordinates so the shipped tool is unchanged. **With Bert's tailnet pin (see
> the instance.yaml block in §"instance.yaml for Bert" below) the operator runs OFF the 10.x LAN —
> on the Bell hub Wi-Fi (192.168.2.x), reaching the hub directly (192.168.2.1) and the box + Mini
> over Tailscale — so the operator survives a `/1` collapse and recovers over the tailnet.**
>
> **What this session added on top of FIX-b:** `net._out_of_band_host()` (reads `devices.mini.host`,
> strips the `user@` prefix for the bare-TCP probe → defaults to `_OUT_OF_BAND_HOST` = `10.0.0.10`);
> `net._out_of_band_reachable()` now probes `_out_of_band_host()` instead of the hardcoded constant.
> Before this, the gate's box leg was config-driven (FIX-b) but its **Mini leg was still
> LAN-hardcoded to `10.0.0.10`** — a fail-closed AND-gate `--force` can't waive — so from the off-LAN
> perch `10.0.0.10` (behind the Firewalla NAT) was unreachable and the gate **refused the very
> cutover FIX-b enables**. That asymmetry is now closed.
>
> **Gate (independently re-run this session):** `.venv/bin/ruff check .` → All checks passed;
> `.venv/bin/mypy sanctum_cli` → no issues in 93 files; `.venv/bin/pytest -q` → **1442 passed, 3
> skipped** (was 1438 after FIX-b → +4 net new gate tests; the 3 skips are the opt-in
> `SANCTUM_LIVE_{FIREWALLA,ORBI,HUB}` smokes). Mutation-checked the off-LAN gate tests non-vacuous
> (revert the Mini leg → 2 go red for the right reason).
>
> **Still open (recovery, separate from FIX-b — for the attended morning batch):**
> `_verify_recovered_double_nat` is still a SINGLE instant read, NOT the bounded settle/poll FIX-a put
> on the forward path, so the rollback-verify can race the 2–5 min dark window and false-YELLOW (never
> false-green). **Residual (live-verify, non-blocking):** the armor installer's scp/ssh do NOT pass
> `-i <key>` (armor.py `_steps`), so the off-LAN armor deploy to `pi@100.68.36.16` authenticates via
> ssh-agent/ssh config — confirm the box key is agent-loaded on the MBP before `--apply` (it fails
> LOUDLY, raising before DMZ engage, never phantom-green). Also: `devices.firewalla.ssh_user` retargets
> only the armor deploy (the runner hardcodes user `pi`); both resolve to `pi`, so consistent.

Branch: `fix/single-nat-three-safety-fixes`. OFFLINE worktree. No live network, no
`--apply`, no push. Gate after all edits: `make check` (ruff + mypy + pytest via `.venv`,
python3.12 — `uv.real` is missing in this sandbox so `.venv` was built directly).

**Original (3345fa0) handoff text follows — see the UPDATE banner above for what changed since.**

**Independently re-verified 2026-06-27 (this doc-pass session):** `.venv/bin/ruff check .` → clean;
`.venv/bin/mypy sanctum_cli` → no issues in 93 files; `.venv/bin/pytest -q` → **1431 passed, 3
skipped** (the 3 are opt-in `SANCTUM_LIVE_{FIREWALLA,ORBI,HUB}` smokes). [STALE as of the banner —
now 1442.] Recovery-transport gap re-confirmed by inspection: `intents._DmzRollbackProvider.rollback`
re-leases via `self._runner` (LAN, line 484) and verifies via `_verify_recovered_double_nat(self._runner)`
(LAN, single read, line 487/410) — **no tailnet primitive in the recovery path**, so automated rollback
is NOT yet LAN-independent. FIX-b stays correctly deferred → cutover is **READY (attended), not
push-button**. [STALE: FIX-b is now LANDED + the runner is config-driven to the tailnet via the pin;
only the rollback-verify single-read remains, per the banner.]

## State of the three 06-26 safety fixes

| Fix | Status |
|-----|--------|
| FIX-1 reboot contract (return path, shape-A) | LANDED at 4bff09a |
| FIX-1 reboot contract (RAISE path, **shape-B**) | LANDED at 71cee0d |
| FIX-2 armor staged BEFORE enable_dmz | LANDED at 4bff09a (ordering pinned by test) |
| FIX-3 fail-closed interlock on Tailscale OOB | LANDED at 4bff09a |
| FIX-a post-reboot settle/poll (no false-fail in the 2–5 min dark window) | **LANDED this session** (see below) |
| FIX-c netmask/route poison gate (a `/1`-poisoned "public" lease never commits green) | **LANDED this session** (see below) |
| FIX-b recovery re-lease over Tailscale (LAN-independent rollback) | **LANDED `c35eb4f` + off-LAN gate Mini leg LANDED this session — see UPDATE banner at top; the "DEFERRED" section below is STALE** |

## instance.yaml for Bert — the tailnet pin that makes the operator off-LAN (FIX-b)

Append to `~/.sanctum/instance.yaml` (the existing `devices.hub.{brand: sagemcom, host: 192.168.2.1}`
block STAYS — the hub is reached DIRECTLY over the Bell Wi-Fi, no tailnet):

```yaml
devices:
  firewalla:
    host: 100.68.36.16        # box over Tailscale (was the 10.0.0.1 LAN gateway)
    ssh_user: pi
  mini:
    host: bert@100.107.112.118  # Mini over Tailscale (was bert@10.0.0.10)
firewalla:
  ssh_key: ~/.openclaw/firewalla/keys/ssh_firewalla
```

What each key retargets:
- `devices.firewalla.host` → the SSH runner (`observe_lease`/`verify` box reads + the recovery
  re-lease) **and** the armor box scp/ssh **and** the OOB recovery gate's box leg.
- `devices.mini.host` → the armor Mini scp/ssh **and** (new this session) the OOB recovery gate's
  Mini leg (the `user@` prefix is stripped for the gate's bare-TCP probe).
- `firewalla.ssh_key` (a PRE-EXISTING seam, not new in FIX-b) is **REQUIRED** so the runner's
  `ssh -i <key> pi@100.68.36.16` uses the box key over the tailnet; without it the runner falls back
  to `~/.ssh/firewalla_ed25519` and a mutating op raises (fail-closed, never phantom-green).

DEFAULT stays LAN (`10.0.0.1` / `pi` / `bert@10.0.0.10`) when these keys are absent — no personal
tailnet IP is a default anywhere; Bert pins them.

## Landed this session — FIX-1 shape-B (CORRECTNESS council must-fix)

**Bug (verified against the installed `sagemcom_api`):** `_reboot_raw` → `__api_request_async` →
`__post`. `__post` (not `__get_response`) makes the return-vs-raise decision. When a
reboot-initiated token rides at the ACTION level under a top-level `XMO_REQUEST_ACTION_ERR`,
`__post` does `raise UnknownException({"description": "XMO_ACTION_CALLBACK_ERR"})`. The old
`reboot()` except-clause only caught connection drops, so it re-raised that as `DeviceError` →
failed `hub_reboot` stage → rails roll back → rollback's own latch-reboot raises the same way →
DMZ left engaged = the 06-26 cascade. (Shape-A — token at the TOP level — is RETURNED, and was
already handled.)

**Fix (`sanctum_cli/devices/sagemcom.py`):**
- `_iter_sah_error_descriptions(exc)` — walks `exc.args` (and one nested level) for SAH
  `description` strings the library packs into its raised typed exceptions.
- `_reboot_initiated_from_exc(exc)` — True iff any of those descriptions is in
  `_SAH_REBOOT_INITIATED` (`XMO_ACTION_CALLBACK_ERR` / `XMO_REBOOTING_ERR`). Mirrors the
  RETURN-path token set so the two never drift.
- `reboot()` except-clause now: connection-drop → ok; else reboot-initiated-token-raised → ok;
  else fail-closed `DeviceError`. Genuine rejections (`AccessRestrictionException`,
  `AuthenticationException`) carry no reboot token → still fail closed.

**Tests (`tests/devices/test_sagemcom_boundary.py`)** — authored from the REAL boundary
(fake only the aiohttp *session*, so the genuine `__post` runs the return-vs-raise decision the
bug lives in; the prior boundary tests mocked `__post` and could only reach the RETURN path):
- `test_reboot_initiated_token_raised_by_real_post_is_success[XMO_ACTION_CALLBACK_ERR|XMO_REBOOTING_ERR]`
- `test_reboot_genuine_rejection_raised_by_real_post_fails_closed` (XMO_ACCESS_RESTRICTION_ERR still raises)

TDD: confirmed RED first (`DeviceError: Sagemcom reboot failed: {'description':
'XMO_ACTION_CALLBACK_ERR'}`), then GREEN. Full gate: **1407 passed, 3 skipped** (opt-in live
smokes), ruff clean, mypy clean (93 files). Baseline before this session was 1404 passed.

## Landed this session — FIX-a settle/poll + FIX-c poison gate (council REVISE → TDD-closed)

Both are pure-brain-at-the-seam + I/O-at-the-boundary, matching the module's `flip` (pure) /
`intents` (I/O) split. TDD: each began RED (hostile fixtures), then GREEN. Full gate after:
**1431 passed, 3 skipped** (was 1407 → +24 tests), ruff clean, mypy clean (93 files).

**FIX-a — bounded post-reboot settle/poll.**
- New PURE brain `flip.settle_poll_decision(observed_class, *, elapsed_s, timeout_s) -> SettleDecision`
  (no clock — elapsed is passed in): `public`→`settled_ok`; `double_nat`→`hard_fail` AT ONCE (DMZ
  did not take — never waited on); a transient (`apipa`/`none`) while `elapsed < timeout`→`keep_polling`
  (re-lease nudge), at/after the bound (`>=`, boundary fails closed)→`hard_fail`. This single function
  IS the "distinguish settling-within-window from hard-fail-past-timeout" contract; it never masks a
  genuine failure.
- `intents._observe_lease` rewritten from a single-shot read into a bounded monotonic-clock poll
  driven by that decision, with **injectable** `now`/`sleep`/`timeout_s`/`poll_interval_s` (real
  defaults `_SETTLE_TIMEOUT_S=360.0` / `_SETTLE_POLL_INTERVAL_S=15.0`, read at call time so they stay
  tunable/monkeypatchable). A transient LAN-SSH blip during the reboot (`RuntimeError` from the
  lease read) reads as `none` = *still settling*, not an instant fail (bounded by the timeout, so a
  genuinely LAN-dark household still correctly hard-fails at the bound). `time.monotonic` (not
  wall-clock) so an NTP step mid-reboot can't corrupt elapsed. Defensive iteration cap
  (`_settle_max_iters`) backstops a frozen clock; the clock is the real bound. The CLI verifier
  (`net._observe_lease_ok`) + the terminal `verify` stage are unchanged — `_run_stage` runs the poll
  to completion BEFORE the verifier reads, so the verifier becomes a fast honest-verify confirmation
  on an already-drained window, not a second racer.
- Tests: 6 pure (`test_flip_machine.py`) + 5 direct driver (`test_single_nat_dmz.py`, REAL
  `ScriptedLeaseRunner` + injected fake clock + no-op sleep) incl. the headline regression — leases
  `[apipa, apipa, public]` read at 30/60/90 s all settle (no false-fail at t+5 s) — plus a
  persistent-transient that hard-fails at the bound (bounded, not infinite), a `double_nat` immediate
  hard-fail (zero re-lease/sleep), and the LAN-blip-as-settling case. The e2e suites drive the REAL
  loop via a tiny-timeout autouse fixture (real monotonic clock + real `time.sleep`, no time-mocking).

**FIX-c — fail-closed netmask/route poison gate.**
- New PURE `flip.evaluate_wan_poison(addr_show, route_show) -> PoisonVerdict` (+ `parse_wan_prefixes`,
  `poison_route_present`): committable IFF the WAN is pinned to `/32` AND no `0.0.0.0/1` route is
  present. A `/1` (or any non-/32) prefix, a present `0.0.0.0/1` route, OR an unparseable/empty
  readback all fail closed — never commit unless the `/32` armor is PROVABLY holding. Authored from
  the consumer's real artifact (`ip -4 -o addr show` / `ip -4 route show`), a different source than
  the producer's `classify_wan_ip`.
- New raw-readback runner tags `wan_addr_cidr` / `wan_routes` in `net/system._FW_MUTATING_REMOTE`
  (+ `_FW_RAW_READBACK_TAGS`, checked before the IPv4-extracting `_FW_READBACK_TAGS`) that KEEP the
  `/PREFIX` + the route table `lease_observe` strips. They live in the mutating map so
  `make_real_runner` routes them and **fail-closes (RAISES, never silent "")** when no fw gateway+key
  on the apply path.
- Wired into `intents._observe_lease` via `_assert_wan_not_poisoned(runner)` at the `settled_ok`
  (public) branch — the only class the old path would have committed. A non-committable verdict raises
  `_StageError` → `guarded_apply` unwinds (disable DMZ + re-lease) → a poisoned-but-public lease can
  never commit green (the exact 06-26 condition). The raw reads fail-closed: if they raise (LAN-SSH
  down at the commit moment) the raise propagates and the rails roll back, because we cannot PROVE
  the armor holds.
- Tests: 8 pure (`test_flip_machine.py`, incl. the 06-26 `/1`+`0.0.0.0/1` fixture, `/32`-but-route-
  survived, `/1`-route-hidden, transient both-prefixes, garbage/empty) + the headline contract
  integration through the REAL `single_nat_dmz` (a `PoisonedPublicRunner` serving a public lease but a
  poisoned netmask/route → rolls back, `armor.installed==0`, both raw tags really read) + the armored
  counterpart that COMMITS + 3 `test_system.py` boundary tests for the raw tags. The existing
  public-success fakes were updated to model the WHOLE contract (a `/32` + clean route), itself a
  Contracts-at-the-Boundary fix.

**Deliberately NOT added (FIX-c defense-in-depth mirror in `net._observe_lease_ok`):** the authoritative
poison gate at the orchestrator's `observe_lease` *stage* fires BEFORE the per-stage verifier runs (and
raises → rollback on a poisoned lease), so a verifier-level mirror is unreachable-redundant in the
integrated flow and would add two more SSH round-trips per attended cutover. The seam-level gate covers
every caller; the runbook's manual "no `0.0.0.0/1`" check (§5) remains as the independent human confirm.

## DEFERRED this session — FIX-b recovery-over-Tailscale (precise rationale)

> **STALE — SUPERSEDED by the UPDATE banner at the top of this file.** FIX-b was LANDED in `c35eb4f`
> (a later session) via a different, cleaner design than the one this section warned against: the box +
> Mini transport is now CONFIG-FIRST (`devices.firewalla.host` / `devices.mini.host`) read at call
> time, so NO personal tailnet node is hardcoded into the shipped `firewalla_wan_via_ssh` read path —
> the leak this section rightly refused to ship. Bert's `~/.sanctum/instance.yaml` pin (see the
> "instance.yaml for Bert" section above) routes the recovery re-lease + reads over the tailnet for
> his haus only. The text below is retained as the historical rationale for why the FIRST attempt was
> deferred; it no longer describes the shipped state. The ONE remaining recovery item is
> `_verify_recovered_double_nat`'s single instant read (still to convert to settle/poll — see below).

**Why deferred (not half-implemented):** the Locate-phase design routes the rollback re-lease +
recovery-read fallback through `net/system._fw_mutate_via_ssh` AND `net/system.firewalla_wan_via_ssh`.
The latter is a **shipped, general-purpose read** used by `net check` / `net optimize` / `verify` for
ALL beta testers — adding the fallback there threads a **hardcoded personal tailnet node**
(`root@100.68.36.16`) into that path, so on ANY LAN read failure (timeout / ssh-255) for a user who has
a Firewalla key, the CLI would attempt SSH to Bert's personal tailnet host. That is blast radius beyond
the cutover, in a tool that ships to testers. Scoping the fallback to only the mutating ops
(`_fw_mutate_via_ssh`) is a HALF-fix — the recovery-verify read (`verify.verify` → `firewalla_wan_via_ssh`)
would still be LAN-bound, so a re-lease that succeeded over the tailnet would then be reported as
unrecovered. The cleaner narrow alternative (a dedicated `recovery_runner` threaded through
`_DmzRollbackProvider` + the CLI `--rollback`) is a *different, more invasive* design than the one
handed off — exactly the kind of plumbing not to land unattended the night before an attended cutover.
Additionally, `root@` over the tailnet for the remote commands (`dhclient`, `ip`, `post_main.sh`) is
verified only by inspection — it cannot be live-exercised offline tonight, and per CLAUDE.md "recovery
code that's subtly wrong is worse than the documented manual compensation."

**Why this is safe to defer:** FIX-a already handles a LAN-dark window gracefully — a `RuntimeError`
from the LAN-bound lease read reads as "still settling," bounded by the 6-min timeout, so it
**fail-closes (hard_fail → rollback), never false-commits**. The manual compensation is already in the
DERISKED runbook (§8 "if `--rollback` cannot reach the box, finish by hand over `root@100.68.36.16`" +
§9 break-glass Tailscale-first), the cutover is ATTENDED with Bert present, and deferring (b) leaves no
sharp edge — only the documented manual Tailscale step.

**To land (b) later (attended):** prefer a dedicated recovery transport scoped to the rollback path only
(no change to the general `firewalla_wan_via_ssh` read path) so the personal tailnet node never leaks
into `net check`/`verify`. Reuse `interlock._ts_ssh_argv` (already `remote`-parameterized,
publickey-only, argv-list, hostile-safe) for the envelope; add a `run_over_tailnet(remote) -> (exit,
stdout)` sibling of `_run_probe`. Fall back ONLY on a raised transport error / ssh-255 (a non-255 remote
exit is the box's own verdict — never fall back on it; the zero-masking property means a misclassified
255 reproduces on the tailnet and raises, so the fallback can't mask a real box-side failure). LIVE-verify
`root@100.68.36.16` runs the recovery commands during the attended dry-run before trusting it.

**Land in the SAME batch (the second recovery must-fix):** convert `_verify_recovered_double_nat`
from its current single instant read into the same bounded settle/poll as FIX-a (reuse
`flip.settle_poll_decision` + a monotonic deadline). Today the rollback reboots the hub
(`intents.py` ~line 474) and then reads ONCE, so it can race the same 2–5 min post-reboot dark
window FIX-a closed on the forward path and false-report "rollback INCOMPLETE / manual recovery" on
a rollback that would have settled. It errs SAFE (false-yellow, never false-green), so it is fine
for tomorrow's ATTENDED run, but it must become a poll when the recovery transport (b) lands.

## Remaining code follow-ups (council REVISE — NOT landed; compensated operationally in the runbooks)

These do NOT re-create the 06-26 strand (FIX-2 + FIX-3 hold), but they degrade a failed `--apply`
toward a yellow "verify + recover by hand" state. Land deliberately with TDD; they touch the
runner timing contract and the recovery transport, so they were left for an attended morning pass
rather than rushed the night before the cutover.

1. ~~**No post-reboot settle/poll (Rollback/Recovery).**~~ **LANDED this session as FIX-a** — the apply
   `observe_lease` stage now polls through the dark window (bounded, monotonic-clock, fail-closed at the
   bound). NOTE: the *rollback-verify* read (`_verify_recovered_double_nat`) is still a single instant
   read — that half of the original item is folded into the (b) deferral (the rollback recovery path is
   what (b) reworks). The forward-apply false-fail is closed.

2. **Recovery re-lease rides the LAN (Rollback/Recovery).** ~~DEFERRED~~ → **LANDED (FIX-b, `c35eb4f`)
   + off-LAN gate Mini leg this session.** The re-lease + box reads + the OOB gate (both legs) are now
   config-first; Bert's tailnet pin routes them over Tailscale, so automated `--rollback` reaches the
   box over the tailnet from the off-LAN perch. **Still open in this item:** only the
   `_verify_recovered_double_nat` SINGLE instant read — convert to the bounded settle/poll FIX-a uses
   (errs SAFE / false-YELLOW today, never false-green). Runbook §8/§9 manual Tailscale recovery remains
   the belt-and-suspenders independent confirm.

3. ~~**`observe_lease` doesn't inspect the netmask/route (Rollback/Recovery).**~~ **LANDED this session
   as FIX-c** — `intents._observe_lease` now reads the prefix + route table (the new `wan_addr_cidr` /
   `wan_routes` raw-readback tags) and rejects a `/1` / a `0.0.0.0/1` route / an unprovable readback via
   `flip.evaluate_wan_poison`, fail-closed. The runbook's manual `poison` check (§5) remains as the
   independent human confirm.

4. **Compound confirm prompt (Human-Factors, optional).** `net.py` asks
   `"{plan}\nAre you at the box (not remote)?"` — one `y` waives both "read the plan" and "I'm at
   the box". Optional: split into two prompts so a fat-fingered `y` can't waive physical presence.
   (Runbook compensates by making the `n`-if-remote rule explicit + bold.)

5. **Operational verifications (strand-prevention must-fix — for the runbook/preflight, not code):**
   prove the Tailscale path is LAN-INDEPENDENT (`tailscale ping 100.68.36.16` → relayed/non-10.x),
   and prove the `/32` armor is EFFECTIVE (not just present) after staging — both are in the
   PREFLIGHT checklist + the DERISKED runbook §3/§5.

## Deliverables written this session
- `~/.sanctum/runbooks/2026-06-27-single-nat-cutover-DERISKED.md` (canonical attended procedure)
- `~/.sanctum/runbooks/2026-06-27-single-nat-PREFLIGHT-checklist.md` (pre-trigger gates)
- This handoff.
- SUPERSEDED banners added to the 06-25 + 06-26 runbooks.
