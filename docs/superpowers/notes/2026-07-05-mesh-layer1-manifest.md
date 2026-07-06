# Sanctum Mesh — Layer 1: module map, seams, and the acceptance E2E

**Date:** 2026-07-05 · **Branch merged:** `feat/mesh-layer1` · **Spec:** `docs/superpowers/specs/2026-07-05-sanctum-mesh-layer1-design.md`

Layer 1 lets an independent haus join the open Sanctum mesh and **pull + verify + seed council champions** BitTorrent-style: content-addressed, signed, eval-gated, sandboxed. It absorbs task #116 (the one-command Tailscale-on-box join). This note is the maintainer's map — what each unit does, the injected seams, and the ONE real acceptance test that needs a second box.

## Module map (`sanctum_cli/mesh/`)

| File | Responsibility | Pure? |
|---|---|---|
| `types.py` | Value types: `ArtifactKind`, `MeshIdentity`, `ChampionManifest` (to/from_dict), `ArtifactRef`, `Verdict`. | pure |
| `identity.py` | Mint/persist/sign/verify a mesh identity. `Signer` **seam**; `MeshIdentityStore` (mint-if-absent, 0600); `LoadedIdentity` (never reprs the private key). | pure + seam |
| `artifact.py` | Content-address (`content_hash` — sorted-file merkle), `build_manifest` (sign `hash + canonical bytes`), `verify_manifest` (hash **then** signature), `verify_signature` (signature-only, for the pipeline's separate attribution). `SigningIdentity`/`VerifyFn` **seams**. | pure + seam |
| `discovery.py` | `Discovery` — tracker-primary, DHT-fallback (degrades on error **or** empty). `DiscoveryTransport` **seam**. | pure + seam |
| `verify.py` | The trust boundary: `adopt(ref, verify_manifest, eval_gate, sandbox, promote, baseline)` runs **hash → signature → eval → sandbox → promote**, short-circuiting on the first failure; the local champion is authoritative and is never replaced on any failure. `ManifestVerifier`/`EvalGate`/`Sandbox` **seams** + `SandboxResult`. | pure + seam |
| `seed.py` | `seed(dir, identity, discovery, …)` — sign + announce a local champion **iff** it beats the local baseline (the mirror of adopt's eval gate). | pure + seam |
| `adapters.py` | The **real** adapters behind the seams (below). | real boundary |
| `tracker.py` | `TrackerRegistry` (pure dict store) + `HttpTrackerTransport` (httpx client) + `build_tracker_app`/`serve` (aiohttp loopback server). | real boundary |
| `commands/mesh.py` | `sanctum mesh join \| status \| pull \| seed`; honest-verify recaps; the `_build_*` builders wire the real adapters. | CLI |

## The seams and their real adapters

Every external dependency is an injected `typing.Protocol` (or a plain callable), so the whole pipeline is unit-tested with fakes and the real boundaries are swapped in at the builder layer:

| Seam (Protocol) | Real adapter (`adapters.py` / `tracker.py`) | Notes |
|---|---|---|
| `identity.Signer` | `Ed25519Signer` | Real `cryptography` Ed25519, hex keys/sigs, `verify` never raises. **Interim** — the Layer-1 target is **ML-DSA-65 (post-quantum)** behind this same seam when a Python ML-DSA binding is pinned. |
| `verify.ManifestVerifier` | `BoundManifestVerifier(store, verify_fn)` | `verify_hash` recomputes `content_hash`; `verify_signature` via `artifact.verify_signature`. A missing/malformed artifact is a clean **False** (failed hash gate), never a crash. |
| `verify.EvalGate` | `AutoresearchEvalGate(store, EvalRunner)` + `mlx_eval_runner` | Runs the tiered autoresearch eval on the adapter; non-zero exit → `LocalError`, not a raw traceback. |
| `verify.Sandbox` | `VmAirgapSandbox(store, VmRunner)` + `vm_airgap_runner` | Ships the adapter to the air-gapped VM, loads it under `unshare -n`, probes, watches for egress. **Fails closed**: a down VM / ambiguous probe raises — it can never read as "clean, no egress." |
| `discovery.DiscoveryTransport` / CLI `MeshDirectory` | `HttpTrackerTransport(base_url)` | Honest-verify: a down tracker / 5xx / bad body **raises `LocalError`**; the only quiet "empty" is a clean 404 on `find` → `None`. |
| `verify.adopt`'s `promote` | `make_promote(store, record)` | Refuses to promote bytes it does not have (`LocalError`); `record` persists `mesh.champion` best-effort. |
| content store | `LocalArtifactStore(base)` | `path_for(ref)` maps a `sha256:` id → a local path; guards the hash against a non-hex/traversal value. Single-box: seeder and puller share the box, so the byte-hop is local; a 2-box `HttpArtifactFetcher` drops in behind the same shape. |

Config (instance.yaml `mesh.*`): `tracker_url` (default `http://127.0.0.1:8765`), `artifact_dir` (`~/.sanctum/mesh/artifacts`), `identity_dir`, `sandbox_host` (unset ⇒ the sandbox gate fails closed at pull time), `label`, `baseline`.

## What the single-box drill proves (`tools/mesh_e2e_drill.py`)

`.venv/bin/python tools/mesh_e2e_drill.py` → **8/8**, exit 0. It stands up the REAL loopback tracker (aiohttp) and drives it with the REAL `HttpTrackerTransport` over real HTTP, mints a REAL Ed25519 identity, content-addresses + signs a champion, then:

* **seed → announce → discover** round-trips through the real tracker (catalog + `find` return a real `ArtifactRef`);
* **happy path**: `adopt` promotes a good champion — `hash ✓` + `signature ✓` (real Ed25519) + `eval ✓` + `sandbox ✓` → `promote`;
* **four real gate rejections**, each keeping the local champion: (a) tampered artifact → `hash`; (b) forged signature → `signature`; (c) below-baseline score → `eval`; (d) egress attempt → `sandbox` (producer flagged);
* **invariant**: exactly one promotion across the whole run.

**Honestly bounded (the two infra-gated boundaries, labeled in the transcript):**
- the **eval runner** is a fixed stand-in — the real `AutoresearchEvalGate` *interface* + `beats_baseline` logic run, but the full 109-case eval + 35B load is the nightly autoresearch path, not the drill;
- the **sandbox probe** is a VM-PENDING stub on the happy path — the air-gapped VM was down; the real `unshare -n` egress probe is covered by the acceptance test below.

## THE acceptance test (requires a second haus/box) — run attended

This is the one thing a single box cannot prove: the cross-haus P2P byte-hop + the real VM sandbox promoting a real peer champion.

1. On **box B**: `sanctum mesh join` (its own free tailnet + the shared tracker), then `sanctum mesh seed <champion-dir> --base-model <m> --score tiered=<s>` — signs + announces to the shared tracker.
2. On **box A** (with `mesh.sandbox_host` set to a live air-gapped VM): `sanctum mesh status` shows B's champion; `sanctum mesh pull <sha256:…>`.
3. **Confirm** the pipeline fires for real: hash + signature verify against B's identity; the eval gate runs the local harness; the **VM sandbox loads the adapter under `unshare -n` and reports no egress**; only then does it promote. Flip one input each run (tamper a byte, corrupt the sig, drop the score below baseline, point the adapter at a probe that beacons out) and confirm the matching gate rejects and A's champion is kept.

Until a second haus exists, the single-box drill + the unit suite (`pytest tests/mesh`, 148 passed) are the standing proof; this acceptance test is the promotion gate for calling Layer 1 "field-proven cross-haus."
