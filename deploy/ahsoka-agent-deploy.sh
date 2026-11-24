#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# ahsoka-agent-deploy.sh — one-command deploy of a full Ahsoka-class agent onto
# an adopted sanctum satellite. The mom-friendly story is TWO artifacts:
#
#   1. The signed/notarized `sanctum node` .pkg (AirDrop → double-click) enables
#      SSH and authorizes the console — that part already ships (see
#      `sanctum node bootstrap-script --format pkg`). Big blobs (a 4GB model)
#      and secrets can never ride inside a .pkg anyway.
#   2. THIS script, run from the console against the adopted node, installs the
#      complete agent: OpenClaw runtime, local brain daemon, agent identity,
#      native MCP hands with guards, and the eval harness.
#
# Usage:  deploy/ahsoka-agent-deploy.sh <ssh-host> [agent-name]
# Required environment (the script refuses, before touching the node, without it):
#   SANCTUM_BRAIN_CLIENT_CERT / _KEY / _CA   paths ON THE NODE of the client
#       certificate, key and CA the agent presents to the brain over mTLS
#   SANCTUM_BRAIN_PROVIDER_REQUEST   local JSON file holding the OpenClaw provider
#       `request` block (the {"tls": {...}} client settings), exactly as OpenClaw
#       expects it; its schema is not guessed here
# Optional: SANCTUM_BRAIN_TLS_HOST   the node's tailnet IPv4 for the brain's
#       second --tls-host (default: `tailscale ip -4` on the node; refuses if absent)
# The brain is sanctum-mlx, mTLS only (--no-plain) on 127.0.0.1:6502. No plain
# HTTP listener is ever started and nothing is pointed at one.
# Idempotent: every step checks before it changes; safe to re-run.
# Generic satellite agent deployment (see docs for satellite setup).
# Needs on the node afterwards, from the operator:
# an HA long-lived token at ~/.sanctum/secrets/ha-token (0600).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
HOST="${1:?usage: ahsoka-agent-deploy.sh <ssh-host> [agent-name]}"
NAME="${2:-ahsoka}"
if [[ ! "$HOST" =~ ^([A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || [[ "$HOST" == -* ]] || [[ "$HOST" == *@-* ]] || [[ "$HOST" == *..* ]]; then
  echo "error: invalid ssh-host: $HOST" >&2
  exit 2
fi
if [[ ! "$NAME" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; then
  echo "error: invalid agent-name: $NAME" >&2
  exit 2
fi
BRAIN_PORT=6502
CLIENT_CERT="${SANCTUM_BRAIN_CLIENT_CERT:-}"
CLIENT_KEY="${SANCTUM_BRAIN_CLIENT_KEY:-}"
CLIENT_CA="${SANCTUM_BRAIN_CLIENT_CA:-}"
REQUEST_FILE="${SANCTUM_BRAIN_PROVIDER_REQUEST:-}"
TLS_HOST="${SANCTUM_BRAIN_TLS_HOST:-}"
for v in CLIENT_CERT CLIENT_KEY CLIENT_CA; do
  if [[ -z "${!v}" || ! "${!v}" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "error: SANCTUM_BRAIN_$v must be an absolute path on the node (the agent's mTLS client file); refusing to deploy a brain without it" >&2
    exit 2
  fi
done
if [[ -z "$REQUEST_FILE" || ! -f "$REQUEST_FILE" ]] || ! python3 -c 'import json, sys; r = json.load(open(sys.argv[1])); sys.exit(0 if isinstance(r, dict) and isinstance(r.get("tls"), dict) else 1)' "$REQUEST_FILE" 2>/dev/null; then
  echo "error: SANCTUM_BRAIN_PROVIDER_REQUEST must name a JSON file with the OpenClaw provider request block ({\"tls\": {...}}); refusing to wire the agent to the brain without client TLS settings" >&2
  exit 2
fi
if [[ -n "$TLS_HOST" && ! "$TLS_HOST" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo "error: invalid SANCTUM_BRAIN_TLS_HOST: $TLS_HOST" >&2
  exit 2
fi
REQUEST_B64="$(python3 -c 'import base64, json, sys; print(base64.b64encode(json.dumps(json.load(open(sys.argv[1]))).encode()).decode())' "$REQUEST_FILE")"
OPENCLAW_VER="2026.7.1-2"            # pin: last version live-fire-verified on a satellite
MODEL_REPO="mlx-community/Qwen2.5-7B-Instruct-4bit"   # bake-off winner (12/12 tools, FR, fits 16GB)
MODEL_DIR=".sanctum/models/Qwen2.5-7B-Instruct-4bit"
SAT_MIRROR="${SANCTUM_SATELLITE_MIRROR:-$HOME/.sanctum-satellites/$NAME}"
say(){ printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
run_remote(){ ssh -o BatchMode=yes -o ConnectTimeout=10 -- "$HOST" /bin/sh -s -- "$@"; }

say "0. preflight ($HOST)"
run_remote <<'EOF'
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
command -v node >/dev/null || { echo "MISSING node — brew install node first"; exit 10; }
command -v python3.12 >/dev/null || { echo "MISSING python3.12 — brew install python@3.12"; exit 11; }
mkdir -p "$HOME/.sanctum/models" "$HOME/.sanctum/logs" "$HOME/.sanctum/secrets" "$HOME/.sanctum/bin" "$HOME/.sanctum/agents"
echo preflight-ok
EOF

say "1. OpenClaw runtime ($OPENCLAW_VER)"
run_remote "$OPENCLAW_VER" "${OPENCLAW_VER%%-*}" <<'EOF'
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
OPENCLAW_VER="$1"
OPENCLAW_MAJOR="$2"
openclaw --version 2>/dev/null | grep -qF -- "$OPENCLAW_MAJOR" || npm install -g "openclaw@$OPENCLAW_VER"
openclaw --version | head -1
EOF

say "2. local brain: model + venv + daemon"
run_remote "$MODEL_DIR" "$MODEL_REPO" "$NAME" "$TLS_HOST" "$BRAIN_PORT" "$CLIENT_CERT" "$CLIENT_KEY" "$CLIENT_CA" <<'EOF'
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
MODEL_DIR="$1"
MODEL_REPO="$2"
NAME="$3"
[ -d "$HOME/.sanctum/mlx-venv" ] || python3.12 -m venv "$HOME/.sanctum/mlx-venv"
"$HOME/.sanctum/mlx-venv/bin/python" -c 'import mlx_lm' 2>/dev/null || "$HOME/.sanctum/mlx-venv/bin/pip" install -q -U pip mlx-lm
[ -d "$HOME/$MODEL_DIR" ] || "$HOME/.sanctum/mlx-venv/bin/python" -c 'import sys; from huggingface_hub import snapshot_download; snapshot_download(sys.argv[1], local_dir=sys.argv[2])' "$MODEL_REPO" "$HOME/$MODEL_DIR"
echo brain-assets-ok
# mTLS only: the brain is sanctum-mlx with --no-plain; nothing listens on plain HTTP.
TLS_HOST="$4"; PORT="$5"; CCERT="$6"; CKEY="$7"; CCA="$8"
if [ -z "$TLS_HOST" ]; then
  TS="$(command -v tailscale || true)"; [ -n "$TS" ] || TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  TLS_HOST="$("$TS" ip -4 2>/dev/null | head -1)" || TLS_HOST=""
fi
case "$TLS_HOST" in
  [0-9]*.[0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "REFUSING: no tailnet IPv4 for the brain (tailscale ip -4 gave none; pass SANCTUM_BRAIN_TLS_HOST)"; exit 12 ;;
esac
[ -x "$HOME/.sanctum/bin/sanctum-mlx" ] || { echo "REFUSING: ~/.sanctum/bin/sanctum-mlx is missing or not executable"; exit 13; }
for f in "$HOME/.sanctum/certs/server.crt" "$HOME/.sanctum/certs/server.key" "$HOME/.sanctum/certs/ca.crt" "$CCERT" "$CKEY" "$CCA"; do
  [ -f "$f" ] || { echo "REFUSING: missing certificate file $f"; exit 14; }
done
# LaunchDaemon (system domain — survives no-login boots). Needs sudo on the node once.
PLIST_PATH="/Library/LaunchDaemons/com.sanctum.${NAME}-brain.plist"
if sudo test -f "$PLIST_PATH" 2>/dev/null; then
  # an older daemon may still be plain HTTP: never keep one
  if sudo grep -q -e '--no-plain' "$PLIST_PATH" && ! sudo grep -q -e '<string>--port</string>' "$PLIST_PATH"; then
    echo daemon-exists
  else
    echo "REFUSING: $PLIST_PATH is not an mTLS-only (--no-plain) brain; replace it first"; exit 15
  fi
else
  sudo tee "$PLIST_PATH" >/dev/null <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.sanctum.${NAME}-brain</string>
<key>ProgramArguments</key><array>
<string>${HOME}/.sanctum/bin/sanctum-mlx</string>
<string>--model</string><string>${HOME}/${MODEL_DIR}</string>
<string>--no-plain</string>
<string>--tls-host</string><string>127.0.0.1</string>
<string>--tls-host</string><string>${TLS_HOST}</string>
<string>--tls-port</string><string>${PORT}</string>
<string>--tls-cert</string><string>${HOME}/.sanctum/certs/server.crt</string>
<string>--tls-key</string><string>${HOME}/.sanctum/certs/server.key</string>
<string>--tls-client-ca</string><string>${HOME}/.sanctum/certs/ca.crt</string>
<string>--temp</string><string>0.0</string>
<string>--max-prompt-tokens</string><string>32768</string>
<string>--metal-cache-limit-mb</string><string>512</string>
<string>--metal-memory-limit-mb</string><string>10000</string>
<string>--metal-wired-limit-mb</string><string>8000</string>
<string>--prompt-cache-slots</string><string>2</string></array>
<key>EnvironmentVariables</key><dict><key>HF_HUB_OFFLINE</key><string>1</string>
<key>RUST_LOG</key><string>info</string>
<key>SANCTUM_MLX_FUSED_MLP</key><string>1</string>
<key>SANCTUM_MLX_FUSED_QKV</key><string>1</string>
<key>SANCTUM_MLX_KV_QUANT</key><string>8</string>
<key>SANCTUM_MLX_SSM_LADDER</key><string>8</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>UserName</key><string>${USER}</string>
<key>StandardOutPath</key><string>${HOME}/.sanctum/logs/${NAME}-brain.log</string>
<key>StandardErrorPath</key><string>${HOME}/.sanctum/logs/${NAME}-brain.log</string>
</dict></plist>
PL
fi
sudo launchctl bootstrap system "$PLIST_PATH" 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s --max-time 3 --cacert "$CCA" --cert "$CCERT" --key "$CKEY" "https://127.0.0.1:${PORT}/v1/models" >/dev/null && { echo brain-serving; break; }; sleep 6; done
EOF

say "3. agent: provider + identity + guards"
run_remote "$MODEL_DIR" "$NAME" "$BRAIN_PORT" "$REQUEST_B64" <<'EOF'
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
MP="$HOME/$1"
NAME="$2"
PROVIDER_ID="${NAME}-local"
BASE_URL="https://127.0.0.1:$3/v1"
PROVIDER_JSON="$(python3 -c 'import base64, json, sys; print(json.dumps({"baseUrl": sys.argv[3], "apiKey": "no-key", "api": "openai-completions", "request": json.loads(base64.b64decode(sys.argv[4])), "models": [{"id": sys.argv[1], "name": f"{sys.argv[2]} local brain"}]}))' "$MP" "$NAME" "$BASE_URL" "$4")"
if openclaw config get "models.providers.${PROVIDER_ID}" >/dev/null 2>&1; then
  # an existing provider must already be the mTLS endpoint: never leave one on plain HTTP
  openclaw config get "models.providers.${PROVIDER_ID}.baseUrl" 2>/dev/null | grep -qF -- "$BASE_URL" || \
    { echo "REFUSING: provider ${PROVIDER_ID} does not point at $BASE_URL; fix it first"; exit 16; }
else
  openclaw config set "models.providers.${PROVIDER_ID}" "$PROVIDER_JSON" --strict-json
fi
openclaw agents list --json 2>/dev/null | python3 -c 'import json, sys; sys.exit(0 if any(a.get("id") == sys.argv[1] for a in json.load(sys.stdin)) else 1)' "$NAME" || \
  openclaw agents add "$NAME" --model "${PROVIDER_ID}/${MP}" --workspace "$HOME/.sanctum/agents/$NAME/workspace" --non-interactive
IDX="$(openclaw config get agents.list 2>/dev/null | python3 -c 'import sys, json; print([a.get("id") for a in json.load(sys.stdin)].index(sys.argv[1]))' "$NAME")"
openclaw config set "agents.list[$IDX].tools.profile" minimal
openclaw config set "agents.list[$IDX].tools.alsoAllow" '["bundle-mcp"]' --strict-json
openclaw config set "agents.list[$IDX].tools.deny" '["exec","read","write","edit","process"]' --strict-json
openclaw config set "agents.list[$IDX].tools.loopDetection" '{"enabled":true,"historySize":12,"warningThreshold":2,"criticalThreshold":3,"globalCircuitBreakerThreshold":5}' --strict-json
openclaw config set agents.defaults.timeoutSeconds 240
openclaw config validate | tail -1
EOF
# identity + mandate: prefer the canonical hub mirror if present on the console
if [ -d "$SAT_MIRROR/agents/$NAME/workspace" ]; then
  say "3b. identity from hub mirror"
  run_remote "$NAME" <<'EOF'
mkdir -p "$HOME/.sanctum/agents/$1/workspace"
EOF
  scp -q -r -- \
    "$SAT_MIRROR/agents/$NAME/workspace/AGENTS.md" \
    "$SAT_MIRROR/agents/$NAME/workspace/IDENTITY.md" \
    "$SAT_MIRROR/agents/$NAME/workspace/SOUL.md" \
    "$HOST:.sanctum/agents/$NAME/workspace/" && echo "identity files pushed"
  run_remote "$NAME" <<'EOF'
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
openclaw agents set-identity --agent "$1" --from-identity --workspace "$HOME/.sanctum/agents/$1/workspace" >/dev/null && echo identity-synced
EOF
else
  echo "NOTE: no hub mirror found — copy AGENTS.md/IDENTITY.md/SOUL.md into the workspace and run agents set-identity"
fi

say "4. native MCP hands (whitelisted HA verbs + call-cap)"
if [ -f "$SAT_MIRROR/bin/ha_mcp.py" ]; then
  scp -q -- "$SAT_MIRROR/bin/ha_mcp.py" "$HOST:.sanctum/bin/ha_mcp.py"
else
  echo "WARN: ha_mcp.py not in mirror — copy it manually"; fi
run_remote "$NAME" <<'EOF'
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
NAME="$1"
[ -d "$HOME/.sanctum/ha-mcp-venv" ] || python3.12 -m venv "$HOME/.sanctum/ha-mcp-venv"
"$HOME/.sanctum/ha-mcp-venv/bin/python" -c 'import mcp' 2>/dev/null || "$HOME/.sanctum/ha-mcp-venv/bin/pip" install -q -U pip 'mcp[cli]'
openclaw mcp list 2>/dev/null | grep -qF -- "${NAME}-ha" || \
  openclaw mcp add "${NAME}-ha" --command "$HOME/.sanctum/ha-mcp-venv/bin/python" --arg "$HOME/.sanctum/bin/ha_mcp.py" --env HA_URL=http://127.0.0.1:8123 --connect-timeout 20
touch "$HOME/.sanctum/ha-map.env"
echo mcp-ok
EOF

say "5. smoke test (dry-run — nothing physical moves)"
run_remote "$NAME" <<'EOF'
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
HA_DRYRUN=1 openclaw agent --local --agent "$1" --thinking off --timeout 120 --session-id deploy-smoke \
  -m 'Turn on the kitchen lights.' 2>/dev/null | grep -vE '^\[' | head -2
EOF

say "DONE — $NAME deployed on $HOST"
echo "Remaining operator steps: (1) HA long-lived token -> ~/.sanctum/secrets/ha-token (0600);"
echo "(2) map real devices in ~/.sanctum/ha-map.env; (3) run the 48-case eval (ahsoka-eval.py) as the gate."
