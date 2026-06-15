# Endocrine system — deploy bundle (the seventh organ)

These are the **daemon-side** artifacts for the endocrine system. They are
**STAGED, NOT LOADED** — additive and off-by-default. Nothing here runs until
Bert turns it on, and even when running it changes **no** council seat until a
seat opts in *and* creative mode is dosed.

Source-of-truth is the `sanctum_cli.endocrine` package (ships with the CLI and
rides its test harness). These deploy copies live at `~/.sanctum` per the
secret-rotator pattern (the OneDrive-synced repo tree is not launchd-readable).

## What's here

| File | Deploy target | Role |
|------|---------------|------|
| `com.sanctum.endocrine-gland.plist` | `~/Library/LaunchAgents/` | Gland daemon — one `sanctum endocrine tick` every 120 s (read real signals → step regulator → publish panel). |
| `endocrine-gland-sentinel.py` | `~/.sanctum/sentinels/` | Watches the gland; pages Force Flow ONLY on pathological state, damped via `alert-confirm.sh`. |
| `com.sanctum.endocrine-gland-sentinel.plist` | `~/Library/LaunchAgents/` | Runs the sentinel every 300 s. |
| `watchdog-catalog-entry.yaml` | `~/.sanctum/services/endocrine-gland.yaml` | Living-Force monitoring entry (liveness = fresh published panel). |

## Turn-on (Bert runs these — the build does NOT)

```bash
SRC=~/Projects/sanctum-cli/deploy/endocrine

# 1. deploy the daemon-side copies
cp "$SRC/endocrine-gland-sentinel.py"                 ~/.sanctum/sentinels/
cp "$SRC/watchdog-catalog-entry.yaml"                 ~/.sanctum/services/endocrine-gland.yaml
cp "$SRC/com.sanctum.endocrine-gland.plist"           ~/Library/LaunchAgents/
cp "$SRC/com.sanctum.endocrine-gland-sentinel.plist"  ~/Library/LaunchAgents/

# 2. dry-verify BEFORE loading (reads real signals, broadcasts nothing)
sanctum endocrine tick --dry-run
python3 ~/.sanctum/sentinels/endocrine-gland-sentinel.py --self-test

# 3. load the organ + its guard
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sanctum.endocrine-gland.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sanctum.endocrine-gland-sentinel.plist

# 4. confirm it's publishing a (neutral) panel
sanctum endocrine panel
```

## Dose creative mode (the headline control)

```bash
sanctum endocrine creative            # dose until explicitly calmed
sanctum endocrine creative --ttl 3600 # auto-expire after 1 h
sanctum endocrine calm                # back to the resting baseline
```

## Opt a council seat in (per-seat, env-gated)

```bash
SANCTUM_ENDOCRINE=1 sanctum council "give me three wild angles on X"
```

With `SANCTUM_ENDOCRINE` unset (the default) the council is byte-identical to
today. VM-wide live-council rollout is a separate, documented opt-in: set
`SANCTUM_ENDOCRINE=1` in the seat's launch env and point
`CHITTI_BASE_URL`/`FORCE_FLOW_URL` at the VM endpoints — do **not** mutate the
live `openclaw.json` seats; subscribe one seat, tune on live signals, then widen.

## Subscription-first guarantee

Creative mode engages MAX diversity across **subscription** seats only
(`receptor.diversity_seats` excludes every `metered` id). OpenRouter is never in
the creative path — it stays a fallback. This is enforced by a test
(`test_creative_mode_engages_max_diversity_within_subscription`).
