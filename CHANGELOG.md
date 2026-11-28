# Changelog

All notable changes to `sanctum-cli` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.8] - 2026-11-27

### Added

- **Sanctum Hive Compute Grid (`sanctum hive swarm`)**: collective Apple Silicon GPU & unified RAM collaboration across sovereign Sanctum Hauses with zero remote code execution and zero tokens/coins:
  - **Zero-RCE payload guard + Trust Zones (`open_hive` vs `trusted_circle`)**: strict per-task allowlists (`inference_map`, `eval_benchmark`, `diloco_step`, `pipeline_stage`), 64 KiB payload caps, recursive execution-key/traversal blocking, and mathematical exclusion of `pipeline_stage` from `open_hive`.
  - **Post-Quantum composite signatures**: domain-separated `Ed25519 + NIST FIPS 205 SLH-DSA-SHA2-128s` manifests & results with loopback-locked HTTP coordinator (`127.0.0.1:8788`, fail-closed non-loopback bearer auth, designed behind `sanctum-pq-terminator --psk-file`).
  - **Byzantine quorum & DiLoCo federated LoRA**: multi-node redundant verification (`--redundancy 1..5`) that slashes lying outliers into `probation`, plus low-communication DiLoCo outer-delta aggregation with local improvement gating and `3.0x` trimmed-median L2-norm outlier filtering.
  - **BitTorrent-style reciprocal compute ledger**: tracks contributed vs. consumed `gpu_seconds`, priority tiers (`vanguard`, `contributor`, `seed`, `probation`), and self-proving `sanctum hive swarm drill --live-mlx` verified live against local `sanctum-mlx :1337`.
- **Notarized Rust Engine Suite fallback bumped to `rust-v1.0.4`**: includes `sanctum-pq-terminator --psk-file` and `sanctum-vault-armor` v2 GF(256) Shamir symmetric armor.

## [1.0.7] - 2026-11-27

### Added

- **Post-Quantum & Crypto-Apocalypse S1–S4 Hardening**: ECDSA P-384 365-day local PKI minting, `CompositeSlhDsaEd25519Signer` (NIST FIPS 205 `SLH-DSA-SHA2-128s`), and `sanctum crypto audit` 4-scenario survival scorecard.

## [1.0.6] - 2026-11-26

### Added

- **Unified Sovereign Typography (`Bricolage Grotesque` + `Instrument Sans` + `IBM Plex Mono`, SIL OFL 1.1, 100% local / zero CDN)**:
  - `sanctum ui` (`127.0.0.1:1119`) and local Sovereign Seal SVG crests (`~/.sanctum/seals/sovereign-seal.svg`) now render in the unified Sanctum type stack (`Bricolage Grotesque` display, `Instrument Sans` body, `IBM Plex Mono` telemetry/code).
  - `sanctum haus install` automatically installs the three open-source font casks locally via Homebrew (`font-bricolage-grotesque`, `font-instrument-sans`, `font-ibm-plex-mono`) so every Sanctum node gets matching local typography with zero cloud requests.
- **Apple Notarized Rust Engine Suite (`rust-v1.0.3`, Apple Notary `Accepted`)**: all 32 Developer-ID signed `sanctum-rs` binaries are notarized with Apple's cloud notary service so macOS Gatekeeper accepts them cleanly with zero quarantine warning on fresh Macs.

## [1.0.5] - 2026-11-26

### Added

- **Sovereign Seals (`Seed #1 — First Beyond the Walls` & `Founding Circle #2–#10`)**:
  - `sanctum hive join` now receives and locally persists (`~/.sanctum/seals/sovereign-seal.json`, mode `0600`) an HMAC-SHA256 verifiable, zero-PII Sovereign Seal minted by `https://hive.sanctum.run` — reserving **`Seed #1 — First Beyond the Walls`** for the very first Mac outside the Founder's Fleet (`Seed #0`) and **`Founding Circle #2–#10`** for the next nine.
  - `sanctum hive seal` (`--open`, `--verify CODE`, `--json`): renders your Obsidian & Gold Sovereign Seal in the terminal, exports a zero-dependency local vector SVG crest (`~/.sanctum/seals/sovereign-seal.svg`), and verifies any claim code against `hive.sanctum.run/verify-seal` with zero PII.
  - `sanctum ui`: automatically unlocks the **Sovereign Gold Theme**, header crest badge, and one-click **"Download Sovereign SVG Seal"** export when a local Seal is present (never exposes private `claim_code` or `signature` to `/api/snapshot`).
  - `sanctum bug-report`: automatically includes your public Hive Seal title (zero PII) so Seed #1 and Founding Circle beta reports stand out immediately.

## [1.0.4] - 2026-11-26

### Added

- `sanctum ui`: local zero-cloud web dashboard (`127.0.0.1:1119`, CSRF-protected) surfacing live health of all 12 `sanctum-rs` Rust services, Castellan unified-memory headroom, `$0` subscription proxy status, disk snapshot, one-click redacted bug-report copy, and interactive local Dragon/Council chat over loopback TLS.
- `sanctum bug-report`: one-command diagnostic collector (`--summary`, `--no-open`, `--json`) that scrubs API keys, tokens, emails, phone numbers, home usernames, and private/tailnet IPs, copies the Markdown report to the clipboard, and opens a pre-filled GitHub issue.
- `sanctum haus seats`: inspect and enable `$0` flat-rate subscription proxies (`sanctum-grok-proxy :4200`, `sanctum-agy-proxy :6543`, Claude Max `:3456`) wired directly into `proxyd` with local `council-mlx` fallbacks for full multi-family Jedi Council neurodiversity at `$0` API spend.
- Pre-compiled macOS Apple Silicon Homebrew bottles (`brew install` / `brew upgrade` in ~4 seconds instead of compiling Rust wheels from source).

### Fixed

- `sanctum haus status` and `mlx_health()` now resolve both fresh-hub (`certs/client.crt`) and per-service (`certs/clients/*.crt`) mTLS client certificate layouts.

## [1.0.3] - 2026-11-26

### Added

- `sanctum haus install | status | start | stop | update | heartbeat`: full hub-parity installer for a virgin Apple Silicon Mac — deploys all 32 Apple Developer-ID signed `sanctum-rs` Rust binaries + `mlx.metallib` + `refusal_dirs_38_v2.safetensors` (`rust-v1.0.3`, zero Homebrew dylibs), mints unique per-machine mTLS PKI (`~/.sanctum/certs/`) + local IPC tokens + Keychain `sanctum-proxy-client`, initializes Memory Vault (`~/.sanctum/memory/`, `:18097`) + cathedral KV prewarm + ablation vectors (`:6669`), downloads Qwen3.8-27B + `dragon-2` champion LoRA (`qwen-haus-dragon-193d2d7407ec`) + secondary Coder 9B cathedral (`:3301` on 64 GB+ Macs, or 9B single cathedral on 16 GB), writes portable `proxyd`, Castellan `capacity.yaml`, `msg-bus`, and `instance.yaml` (`role: hub`) configs, and installs up to 16 auto-starting macOS LaunchAgents (`sanctum-presidio`, `sanctum-memory`, `sanctum-mlx`, `sanctum-mlx-coder`, `proxyd`, `sanctum-force-flow`, `sanctum-msg-bus`, `sanctum-chitti`, `sanctum-thalamus`, `sanctum-triage`, `sanctum-pressure-valve`, `sanctumd`, `castellan`, `sanctum-chi`, `endocrine-gland`, `r2d2`).
- `sanctum yoda setup` and `sanctum yoda chat` now default to the native Rust engine (`https://127.0.0.1:4040/v1` via `proxyd` → `sanctum-presidio` → `sanctum-mlx` mTLS `:1337`), injecting `NODE_EXTRA_CA_CERTS=~/.sanctum/certs/ca.crt` and `--cli-path` for `signal-cli`, with automatic fallback to standalone Python Dragon (`--backend dragon`).
- `sanctum dragon install` now defaults to the Rust `sanctum-mlx` runtime (`sanctum haus install`), keeping `--runtime python` available for standalone `mlx-lm` installs.
- Unified `sanctum update` now upgrades `sanctum-cli`, the `sanctum-rs` Rust engine suite (`rust-*`), Dragon champion adapters (`dragon-*`), and Yoda/Signal in one self-tested command.

## [1.0.2] - 2026-11-26

### Added

- `sanctum dragon install | chat | serve | start | stop | status | update`: one-command installer and updater for the Jedi-Trials-verified local Sanctum Dragon (`dragon-2` champion `qwen-haus-dragon-193d2d7407ec` + `mlx-lm 0.32.2` on 32 GB+ Macs, automatic `Qwen3.5-9B-4bit` tier on 16 GB Macs, strict SHA-256 verification and ZipSlip protection).
- `sanctum yoda setup | status | chat | update`: guided Yoda-on-Signal installer (`signal-cli` + `openclaw@latest` + `@openclaw/signal` + isolated `sanctum-yoda` profile + LaunchAgent), with clear dedicated-bot-number guidance and an operator-haus guard so production hubs are never touched.
- Unified `sanctum update`: upgrades `sanctum-cli` via Homebrew plus any installed Dragon champion (`dragon-*` + `mlx-lm`) and Yoda/Signal stack (`signal-cli` + `openclaw@latest`), gated by `sanctum self-test` (use `--cli-only` to skip optional components).
- `sanctum status` now flags both newer `sanctum-cli` and newer Dragon champion releases from `https://hive.sanctum.run/latest.json`, and Hive Pulse anonymously reports `dragon` / `yoda-signal` module slugs when opted in.

## [1.0.1] - 2026-11-26

### Added

- Opt-in Sovereign Hive Pulse (`sanctum hive join | leave | status | stats`) backed by the publicly auditable zero-PII Cloudflare Worker at `https://hive.sanctum.run` (stores anonymous node ID, chip, RAM, macOS version, country code, CLI version, and active module slugs — zero IP, zero hostname, instant erasure via `sanctum hive leave`).
- Interactive opt-in Hive Pulse prompt at the end of `sanctum onboard` (interactive TTY only; never prompts under `--yes` or non-TTY scripts), plus 24h-throttled opt-in heartbeats on `sanctum status` and `sanctum backup`.
- Lightweight zero-ID daily update check (`GET https://hive.sanctum.run/latest.json?v=<version>&os=macos`, max 1×/24h on interactive `sanctum status`; opt out cleanly via `SANCTUM_NO_UPDATE_CHECK=1`).

## [1.0.0] - 2026-11-23

Sanctum 1.0, in beta: we're looking for beta users.

### Added

- Keyless Sigstore OIDC release signing (`cosign sign-blob` `.sigstore.json` bundles) and SLSA build provenance (`actions/attest-build-provenance`) in `.github/workflows/release.yml`, with tag-vs-`pyproject.toml` version verification and closed-loop signature verification prior to GitHub Release upload.
- `sanctum tailnet` LWCR CDHash daemon-skew classifier and upgrade hint; Gemini subscription proxy mode (`sanctum_cli.providers.gemini`).
- `instance.role` declares what a machine is: `satellite` for a contained remote site (a second haus that runs its own local brain and none of the hub services), `hub` for the primary haus. A declared role decides the tier everywhere: model manifests on disk no longer make a satellite look like a hub, and a declared hub is graded as one before its services leave anything on disk. Without a role, the tier is still inferred from what is on disk. Every role reader (the haus gate, `self-test`, `doctor`, `setup`, `service-user`) reads the role the same way, from the raw YAML: it counts only when it is exactly `hub` or `satellite` in a top-level `instance:` block of keys (a `!!set` is not one). A character PyYAML refuses (such as an escape code or a BEL) is read as an unknown character, which is never part of `hub`, `satellite`, `role` or `instance`: one in a comment hides nothing, and one inside a key or value breaks that key or value; where it cannot stand (inside a tag, an anchor or alias name or a block scalar's header, or right after a quoted value, a flow collection or a key's colon) it is a syntax error like any other, so a role after it is not read. Another value that fails the schema does not hide the role, and neither does one that cannot be built (such as `since: 2026-02-30`): the role is then read off the parsed YAML nodes, without building any value. A file PyYAML cannot load as one document (a syntax error, such as a tab indent; an undefined alias; a second document) is read off the parser's events, without following aliases or `<<` merges: a role written before the first syntax error is read; one after it, or only in a second document, is not. A `role` whose value the error cuts off (an unclosed quote, say) reads as no role, but a plain value ends where PyYAML ends it, before the error: at a tab or a `: `, and inside braces also at `,`, `?`, `[`, `]`, `{` or `}`. So `role: satellite<TAB># note`, `role: hub<TAB>x` and `role: hub: x` read as the text before the tab or colon, and `{role: hub]` reads `hub`. A file that is not UTF-8 reads as having no role.
- `sanctum self-test` grades a satellite on its own profile: `tier` is `satellite` in `--json` and the headline, and the hub probes (cathedrals, proxyd, proxy key, Force Flow, service user, chitti, TCC, R2D2, audit log) report n/a instead of failing.
- On a satellite, `self-test` probes the local brain: `GET /models` on the `router.default` backend, using the router's keys (`ca_cert_path`, `client_cert_path` and `client_key_path` for mutual TLS on an `https` url; `api_key_env` names the variable whose value is sent as a bearer token). A 401/403 counts as answering, with a note on the key. Each of these failures says what to fix in one line: a missing or malformed `router:` block (with the expected shape), a `router.default` that is not a string, the pre-release `ca`, `client_cert` or `client_key` keys without their new names (`ca_cert_path`, `client_cert_path`, `client_key_path`; they are never read, so the probe fails with the rename to make instead of running without them), TLS paths on an `http` url, a missing or passphrase-protected key (never an OpenSSL prompt), a handshake that needs a CA or a client certificate, a 404 (base-path hint) and any redirect. The probe ignores proxy settings, follows no redirect and never sends the token anywhere else; the token is trimmed of surrounding whitespace, a value with a control character fails before any request, and the token never appears in the output.
- `self-test` reports a locked Vault Armor volume wherever `~/.sanctum/vault-armor/manifest.json` exists. It runs `/usr/sbin/diskutil` by absolute path.
- The other commands follow the role. On a satellite, `doctor` no longer expects Force Flow or the wave-1 service principal, `service-user check` and `status` report n/a without suggesting an install, `service-user install` refuses with a configuration error (exit 5) because the service principal is hub-only (so do `python -m sanctum_cli.service_user install` and the root step it runs under `sudo`, which reads the role from the operator's own `instance.yaml`), `setup` shows only the basic panes, and the service-principal step of `onboard --recipe operator` prints n/a and writes or asks nothing. Each install path loads an existing `instance.yaml` before it reads the role, so a file that does not load (such as one with `since: 2026-02-30`) stops the install with a configuration error (exit 5) naming the problem, whatever its role, and nothing is installed or written.
- On a satellite, the hub's services are not here, so `council`, `chat`, `code`, `brainstorm`, `endocrine` and `bridge` print the "needs a full Sanctum haus" banner (exit 0) instead of running, and a bare `sanctum` in a terminal prints its status line and no longer opens the council chamber (it prints no banner). The satellite's own mTLS CA (for its local brain), the council and bridge environment variables (`SANCTUM_PROXYD_*`, `SANCTUM_COUNCIL_URL`, `SANCTUM_BRIDGE_URL`) and a bridge Keychain entry no longer pass for the hub there. `SANCTUM_HAUS_ON_SATELLITE=1` (or `true`, `yes`, `on`) restores the usual detection for a satellite that can reach the hub's council and bridge; the banner names it. `screen-time`, `devices`, `schedule`, `agent` and `proxy` keep their own checks (a satellite's `devices.yaml` and `haus.sanctum.*` agents are its own).
- `sanctum init --role satellite|hub` writes the role, and `init` now edits an existing `instance.yaml` in place where it used to report "already exists". A file without an `instance:` block gets one (the name is chosen as for a new file: `--name`, the hostname default under `--yes`, else a prompt). An existing block gets just the requested `name` / `role` changes, printed as old -> new. Comments, key order, a UTF-8 BOM and line endings are kept, the result is re-parsed before it is written (comparing each value shared through an alias once, so aliases that double level after level, `b: [*a, *a]`, `c: [*b, *b]`, ..., stay fast), the write is atomic and keeps the file's mode, and the original is saved to `instance.yaml.bak`. An existing slug never changes (it names the haus's buckets and URLs); a block without one gets a slug derived from its name. A file the editor will not change safely (a flow-style, tagged, anchored or aliased `instance:`, a single-value or list `instance:`, a second `instance:` key, or a key the edit changes that appears twice inside the block) is left untouched and the lines to add by hand are printed; anchors and duplicate keys elsewhere in the file do not stop an edit, since the re-parse must still match; a single-value or list `instance:` is refused before `init` asks for a name. A file that parses but fails the schema exits 5 with the schema error, also when there is nothing to change. Names, paths and values are printed exactly as written, never as Rich markup.
- `sanctum vault-armor status`, `self-test`, `unlock` and `lock` run the same subcommand of the `sanctum-vault-armor` binary (`~/.sanctum/bin/sanctum-vault-armor`, or the path in `SANCTUM_VAULT_ARMOR_BIN`) and print what it reports. `status` is a table of the vault ID, mode, threshold, cipher suite, state (`UNLOCKED & MOUNTED` when the volume is mounted and not locked, else `LOCKED`), volume UUID, mount point and 1Password escrow item, with a row per witness probe the binary returns (`DLEQ OK` or `UNREACHABLE/DENIED`); `--probe-witnesses` is passed on to it. `self-test` prints `PASS` when the binary's self-test exits 0. `unlock` passes on `--wait <seconds>` (default 0) and an optional `--extra-share <file>`, and prints the volume name, mount point and last action. `lock` prints the volume name and UUID. With `--json`, each prints the JSON object the binary printed instead: its output from the first `{` to the end when that is one JSON object, else `{"raw": ...}` holding the output. The binary gets 45 seconds for `status`, 20 for `self-test` and `lock`, and `--wait` plus 20 (at least 30) for `unlock`. A missing binary, one that is there but cannot be run (no execute permission, or not a program this Mac runs), a timeout or a failed run is an `error:` (exit 4); the `fix:` says to build the binary when it is missing, to make it executable (`chmod +x`) or rebuild it when it cannot be run, and points at the break-glass `diskutil apfs unlockVolume SanctumVault` when a run fails.

### Fixed

- `sanctum node resolve` no longer answers for an offline tailnet peer with the roster's LAN `host` (a site address that can be a different machine at another site): it reports the peer offline, and a node with a tailnet name is never reached by a bare LAN address.
- `sanctum node adopt` no longer names a node from the remote machine's own hostname: the name is `--name` or the machine's `~/.sanctum/.node_id` (a mismatch or neither is refused), adopt never overwrites an existing roster row unless it is the same satellite re-adopted explicitly with `--name` (a name that comes only from the machine's `.node_id`, a hub row, or a row for a different machine is refused), and a row it does write is merged and written atomically (temp file, fsync, rename) instead of replaced.
- `deploy/ahsoka-agent-deploy.sh` no longer starts or targets a plain-HTTP brain on :1338: it deploys the mTLS-only `sanctum-mlx` brain (`--no-plain`, `:6502`, the node's `~/.sanctum/certs`), health-checks it over https with the client certificate, points the agent at `https://127.0.0.1:6502/v1`, and refuses (before touching the node) without the client certificate, key, CA and provider TLS settings, and on the node without a tailnet IP or the certificate files, or when an older plain daemon or provider is already there.

### Changed

- `sanctum init --force` saves the file it replaces to `instance.yaml.bak` and writes the new one atomically, keeping the file's mode.
- The service-principal step of `onboard --recipe operator` no longer writes the wave-1 plists or offers the install on a machine with neither a declared hub role nor hub artifacts, where the wave-1 check does not apply. It prints n/a with the reason and points a new hub at `sanctum init --role hub`.

### Fixed

- `sanctum self-test` (`probe_r2d2_heartbeat`) now attaches the local `control-plane-ipc.token` Bearer credential when querying `chitti` (`:2188`), fixing false 401s on hardened Sanctum 1.0 hosts.
- `sanctum service-user` binds the `memory-vault` plist template to `127.0.0.1`; declared `websocket-client` dependency explicitly for `ha-green-bridge`; fixed endocrine gland beat timing (`GLAND_DOWN` deferrals) and cloud restic status timeout.
- `instance.yaml` problems are configuration errors, not tracebacks, wherever the file is read through `config.load()` or `config.load_raw()`: `config validate` and the other commands that load the config, `init`, `service-user install`, the `sanctum node` commands that need the roster, and the reads and writes of `instance.yaml` in `onboard`, the `setup` Setup Assistant, `mesh` and the last step of the `cloud setup` wizards (R2, B2, Google Drive), which saves the new backup target. An unreadable file, one that is not UTF-8 and one nested too deeply for PyYAML are reported by name, and a value PyYAML cannot build (an impossible date such as `2026-02-30`, `!!int ""`, `!!bool maybe`) by file, line and value, each with a fix. With several bad values, the first date, number or true/false value in the file that PyYAML cannot build is named with its own reason, and a `<<` merge key is never blamed for a bad value after it. `config.instance_value()` returns its default for all of these instead of raising, so they no longer crash every command while the CLI imports. The Setup Assistant's family pane reads screen-time's `devices.yaml` the same way: a file it cannot read, one that is not UTF-8, one nested too deeply and a value PyYAML cannot build get an error reply naming the file (and the line), where they used to crash the pane's request.
- An empty `cli:` block in `instance.yaml` (no value, or `~`, as a template whose `cli:` keys are all commented out leaves it) is read as no `cli` settings, each at its default. In 0.15.5 `config.load()` refused it as a schema violation: `config validate`, the `cloud setup` wizards and the other commands that need the config stopped (exit 5), `self-test` failed its config probe, and `brainstorm` and `council` ran without telemetry.
- A `SANCTUM_INSTANCE_FILE` that starts with an unknown `~user` (such as `~nosuchuser/instance.yaml`) is taken as the literal path. In 0.15.5 every command, even `sanctum --version`, stopped with a traceback as the CLI started.
- A read-modify-write of `instance.yaml` no longer replaces a file it could not read with just its own keys (the Setup Assistant's backup pane did this for any file it could not parse; `node adopt`, `ensure-hub` and `declare-peer` and the `onboard` steps for a file whose top level is not a mapping; `mesh` for invalid YAML). It reports the file and leaves it as it was; `mesh`, whose writes are best-effort, prints a note and carries on. The Setup Assistant's AI pane loads `instance.yaml` before it stores a key, so a file that does not load leaves nothing in the Keychain. Its family pane no longer replaces a `devices.yaml` whose top level is a list or a single value with just the family block.
- The last step of the `cloud setup` wizards (R2, B2, Google Drive), which saves the new backup target, is one step shared by all three: it saves the original's exact bytes to `instance.yaml.bak.<stamp>` with the file's own permissions, then replaces the file atomically, keeping its permissions and owner, and writes through a symlinked `instance.yaml`; a read-only one is reported (exit 5) and left as it was. In 0.15.5 the backup was rewritten as text (CRLF line endings were lost), both files got default permissions, and a symlinked `instance.yaml` was replaced by a plain file, leaving the file it pointed to unchanged. A `cli` or `cli.cloud_backup` that is not a block of keys is a configuration error (exit 5) that leaves the file as it was and prints the entry to add by hand; only a key with no value (or `~`), or `{}`, counts as absent, so any other empty or false value is refused too. In 0.15.5 this step silently replaced an empty or false value (`[]`, `''`, `false`, `0`, `0.0`, an empty set) with a block holding the new entry, and any other value that is not keys (a non-empty string, list or set, a non-zero number, `true`) was a traceback, except that B2 and the Google Drive primary slot printed `cli: must be a mapping` for such a `cli`. Each wizard checks the file against the schema when it starts. That check now accepts an empty `cli:` block (no value, or `~`), which the last step then fills, and still refuses every value the last step refuses, so the last step meets one of those only in a file changed while the wizard ran.
- `cloud setup --backend gdrive` writes `instance.yaml` once, so its `instance.yaml.bak.<stamp>` holds the file as it was before the wizard. Filling the primary slot used to write the file twice, usually within the same second, and then the backup of the half-written file replaced the backup of the original.
- `sanctum node names` prints its cheat sheet and exits 0 when `instance.yaml` cannot be read: the live roster is optional, so a dim note in its place names the problem (in 0.15.5 it stopped with a traceback). The cheat sheet's example line `aliases: [alice-mbp]` shows its list, which Rich used to swallow as markup, and roster names print as written.
- A roster row whose `aliases:` is not a list (a single name, a number) counts as having no aliases in `node names` and the other `node` commands that read aliases (`resolve`, `route`, `validate` and the rest), and an alias written as a number is read as text. In 0.15.5 a number in place of the list crashed them, a single name counted as one alias per letter, and a number in the list could crash `node validate`.
- Errors print their `error:` and `fix:` lines exactly as written: a path or value in brackets (`[site]`, `"[bold]x"`) is no longer swallowed as Rich markup, and a long line is soft-wrapped, so a path never breaks mid-word. This covers every command that reports through the CLI's top-level handler, and the `node`, `net`, `link`, `tailnet`, `mesh` and `service-user` commands, which have reporters of their own.
- Renaming the box in `sanctum setup` uses the same in-place editor as `init`: the slug no longer changes with the name, and comments and the other blocks are kept.
- `sanctum service-user install` reports a failure as one `error:` / `fix:` pair with its exit code instead of a traceback.
- `self-test --json` includes `tier` and `not_applicable` when `--only` matches no probe.
- The test suite is hermetic: the suite no longer reads the developer's `instance.yaml`, `devices.yaml` or certificates, and no longer writes the wave-1 plists into the real `~/.sanctum/launchdaemons`. `tests/conftest.py` points `SANCTUM_INSTANCE_FILE` at each test's fake home (and at an absent file during collection), points the TLS CA variables (`SANCTUM_PROXYD_CA`, `SANCTUM_COUNCIL_CACERT`, `SANCTUM_COUNCIL_CA`) at a throwaway CA it mints before collection on every machine, whatever the shell exports, and pins `service_user.operator_home()`. The `devices.yaml` paths are resolved when used rather than at import, and the wave-1 plist test checks that pin before it writes anything, so it fails rather than write into a real home.

### Security

- **CRIT-01 / CRIT-02 / CRIT-04 / H-CLI-2..9 / M-CLI-1..6**: `sanctum` refuses to start from a world-writable path; hardened CSRF/origin checks on `sanctum setup`, fail-closed TLS on `brainstorm`, R2/restic env isolation, `sanctum heal` dir trust, pubkey probe validation, `FSL-1.1-Apache-2.0` license unification, SHA-pinned military-grade CI, and full personal-infrastructure leak scrub.
- `sanctum node resolve`, `node vault-host`, `node route` and `node export-council` find a tailnet machine by its MagicDNS name only. In 0.15.5 they also matched the host name each machine reports for itself, and a machine listed later replaced one listed earlier under the same name unless it was offline, this machine included: any machine on the tailnet, such as a compromised satellite, could set its host name to the hub's (`home`) and take the hub's place, so `node resolve home` and `vault-host` printed the satellite's address as the hop, `route` sent work to it and `export-council` wrote it into the council-router overlay. Now a machine without a MagicDNS name in this tailnet (one shared in from another tailnet included) is not found by a short name, this machine's own MagicDNS name always resolves to itself, and two machines listed under one name are never told apart by which one is online, since a satellite can always be online: resolving that name fails with `ambiguous tailnet name '<name>': 2 tailnet machines claim it (...)`, naming each machine's MagicDNS name and Tailscale ID (exit 3; `vault-host` exits 1), and no other name, `tailscale ip` lookup or configured `host` is tried. A machine tagged `tag:satellite` no longer answers for a roster node that is not a satellite: one whose `type` is anything else (`hub`, `mobile`), or one without a `type` that another node names as its `sync.hub` or that offers `vault_authority`. Resolving such a node fails the same way, with `node '<name>' is not a satellite (...), but tailnet name '<name>' belongs to a machine tagged tag:satellite (...)`; `route` prints that line in place of the address, `export-council` leaves the node's host as written and says why on stderr, and `route` no longer counts that machine, or an ambiguous name, as the node being online. A satellite still resolves to its own machine. A roster `host` that is an IP address is used as written, no longer looked up as a machine named after its first number (in 0.15.5 a machine named `192` answered for `host: 192.0.2.50`). A replacement machine that the control plane named `home-1`, because the old `home` is still listed, no longer answers for `home`: remove the old machine and rename the new one in the Tailscale admin console, or add `home-1` to the node's `aliases:`.

## [0.15.5] - 2026-10-01

### Added

- `sanctum mesh send` and `sanctum mesh receive` move one signed file across the tailnet. The receiver checks the signature and the sha256, saves the file, and does not run it.
- `sanctum mesh join --tribe <key>` puts a friend's Mac on the trusted tribe tag. A haus node that already carries an admin, host, or mobile tag refuses the switch. Champion pull still goes through eval and the sandbox.

### Security

- The `sanctum` command refuses to run (exit 4) when its binary (symlinks followed), or any directory above it, is world-writable; only `sanctum --version`, `sanctum --help` (`-h`) and Typer's shell completion (its `--show-completion` and `--install-completion` options, and the completions a shell asks for) still answer; a mistyped command or option still gets the usage error (exit 2).

## [0.15.4] - 2026-08-07

### Added
- **`sanctum service-user`** — greenfield hive service principal install (user `sanctum`,
  wave-1 LaunchDaemons for proxyd / force-flow / memory-vault). Packaged plists;
  no pre-synced sanctum-config required. Operator onboard gate + self-test + doctor.

## [0.15.3] - 2026-07-24

### Added

- `sanctum upgrade` registry now tracks the primary host's `mlx-finetune/.venv` and `yoda-voice-agent/.tts-venv` (mlx-lm as the drift canary), so the daily sentinel catches MLX drift in the training and voice stacks instead of only the satellite brain venv.

## [0.15.2] - 2026-07-24

### Removed

- `gemini-cli` dropped from the `sanctum upgrade` registry — retired from the haus (2026-07-24); the binary was uninstalled from all machines.

## [0.15.1] - 2026-07-20

### Fixed

- `sanctum upgrade` now invokes a venv's pip as `<venv>/bin/python -m pip` instead of the `bin/pip` console script, whose shebang bakes in the venv's original path — a relocated/renamed venv (e.g. a satellite mlx venv) has a working python but a broken pip binary, which made upgrade report the tool absent. Live-fire-verified on a satellite node.

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

[Unreleased]: https://github.com/ogilthorp3/sanctum-cli/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ogilthorp3/sanctum-cli/releases/tag/v1.0.0
[0.10.1]: https://github.com/ogilthorp3/sanctum-cli/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/ogilthorp3/sanctum-cli/releases/tag/v0.10.0
