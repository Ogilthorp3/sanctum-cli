"""Sanctum endocrine system — the council's slow, broadcast, feedback-regulated
modulator of disposition (and, per Bert's steer, its CREATIVITY regulator).

This is the MISSING organ in the Sanctum body:
  BONES (Rust)   — castellan / proxyd / cathedral / watchdog / chitti
  MUSCLE (Py)    — sentinels, R2D2, feature-organic services  ← the gland lives here
  FASCIA         — vault / bus / discovery
  NERVOUS (4077) — Force Flow fast point-to-point alerts
  IMMUNE         — Living Force / watchdog (detect → heal)
  MIND           — chitti (samskara / memory)
  ENDOCRINE      — THIS: slow, broadcast, homeostatic disposition modulator

Five pieces:
  gland        — the pure homeostatic regulator + Panel math (signal→hormone).
  gland_daemon — the live organ: reads REAL local signals, steps the regulator
                 once per tick, broadcasts the Panel on the bloodstream.
  bloodstream  — the broadcast/read transport: chitti samskara + a listener-free
                 query file + the creative-mode lever file. Discovery-resolved
                 endpoints (no hardcoded literals).
  receptor     — the OPT-IN, OFF-BY-DEFAULT translation layer a council seat uses
                 to turn a Panel into concrete sampling/framing/diversity knobs.

The operator control surface is ``sanctum endocrine`` (panel / creative / calm /
status / tick) in sanctum_cli.commands.endocrine_cmd. The daemon-side deploy
bundle (staged plists, the gland sentinel, the watchdog catalog entry) lives in
deploy/endocrine/ — STAGED, never loaded by the build.

Hard invariants (each enforced by a test in tests/test_endocrine.py):
  • Homeostatic: a damped negative-feedback regulator that CANNOT run away.
  • Additive + off-by-default: a neutral/absent panel changes NOTHING.
  • No hardcoded endpoints: ports come from instance.yaml via config.

Subscription-first (DESIGNED, NOT YET WIRED): receptor.diversity_seats() would
  exclude metered seats, but council_ask() does not yet consult it — it fans out
  over all SEATS. No live path selects seats by panel today, so subscription-first
  is design intent on an isolated, unit-tested helper, not an enforced runtime
  invariant. (See receptor.diversity_seats and deploy/endocrine/README.md.)

Rust-readiness (rust_readiness doctrine — Python while feature-organic):
  Promote the Regulator + Panel math to sanctum-rs (a sibling kosha to
  chitti's MoodTracker) ONCE the update rule, setpoints and signal mapping
  have been stable for ~2 weeks AND the gland is load-bearing for a live
  council seat. The math here is intentionally a 1:1 port of chitti's EWMA
  precedent so that port is mechanical.
"""

from __future__ import annotations
