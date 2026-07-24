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
# Idempotent: every step checks before it changes; safe to re-run.
# Encodes the chalet build of 2026-07-19..24 (see memory: ahsoka-openclaw-agent-live,
# ahsoka-best-brain-shortlist). Needs on the node afterwards, from the operator:
# an HA long-lived token at ~/.sanctum/secrets/ha-token (0600).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
HOST="${1:?usage: ahsoka-agent-deploy.sh <ssh-host> [agent-name]}"
NAME="${2:-ahsoka}"
OPENCLAW_VER="2026.7.1-2"            # pin: last version live-fire-verified on a satellite
MODEL_REPO="mlx-community/Qwen2.5-7B-Instruct-4bit"   # bake-off winner (12/12 tools, FR, fits 16GB)
MODEL_DIR=".sanctum/models/Qwen2.5-7B-Instruct-4bit"
say(){ printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
run(){ ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "export PATH=/opt/homebrew/bin:/usr/local/bin:\$PATH; $*"; }

say "0. preflight ($HOST)"
run 'command -v node >/dev/null || { echo "MISSING node — brew install node first"; exit 10; }'
run 'command -v python3.12 >/dev/null || { echo "MISSING python3.12 — brew install python@3.12"; exit 11; }'
run "mkdir -p ~/.sanctum/{models,logs,secrets,bin,agents} && echo preflight-ok"

say "1. OpenClaw runtime ($OPENCLAW_VER)"
run "export PATH=/opt/homebrew/bin:\$PATH
openclaw --version 2>/dev/null | grep -q '${OPENCLAW_VER%%-*}' || npm install -g openclaw@$OPENCLAW_VER
openclaw --version | head -1"

say "2. local brain: model + venv + daemon"
run "export PATH=/opt/homebrew/bin:\$PATH
[ -d ~/.sanctum/mlx-venv ] || python3.12 -m venv ~/.sanctum/mlx-venv
~/.sanctum/mlx-venv/bin/python -c 'import mlx_lm' 2>/dev/null || ~/.sanctum/mlx-venv/bin/pip install -q -U pip mlx-lm
[ -d ~/$MODEL_DIR ] || ~/.sanctum/mlx-venv/bin/python -c \"from huggingface_hub import snapshot_download; snapshot_download('$MODEL_REPO', local_dir='\$HOME/$MODEL_DIR')\"
echo brain-assets-ok"
# LaunchDaemon (system domain — survives no-login boots). Needs sudo on the node once.
run "sudo test -f /Library/LaunchDaemons/com.sanctum.${NAME}-brain.plist 2>/dev/null && echo daemon-exists || sudo tee /Library/LaunchDaemons/com.sanctum.${NAME}-brain.plist >/dev/null <<PL
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
<key>Label</key><string>com.sanctum.${NAME}-brain</string>
<key>ProgramArguments</key><array>
<string>\$HOME/.sanctum/mlx-venv/bin/python</string><string>-m</string><string>mlx_lm</string><string>server</string>
<string>--model</string><string>\$HOME/$MODEL_DIR</string>
<string>--host</string><string>0.0.0.0</string><string>--port</string><string>1338</string></array>
<key>EnvironmentVariables</key><dict><key>HF_HUB_OFFLINE</key><string>1</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>UserName</key><string>\$USER</string>
<key>StandardOutPath</key><string>\$HOME/.sanctum/logs/${NAME}-brain.log</string>
<key>StandardErrorPath</key><string>\$HOME/.sanctum/logs/${NAME}-brain.log</string>
</dict></plist>
PL
sudo launchctl bootstrap system /Library/LaunchDaemons/com.sanctum.${NAME}-brain.plist 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s --max-time 3 http://127.0.0.1:1338/v1/models >/dev/null && { echo brain-serving; break; }; sleep 6; done"

say "3. agent: provider + identity + guards"
run "export PATH=/opt/homebrew/bin:\$PATH
MP=\$HOME/$MODEL_DIR
openclaw config get models.providers.chalet-local >/dev/null 2>&1 || \
  openclaw config set models.providers.chalet-local \"{\\\"baseUrl\\\":\\\"http://127.0.0.1:1338/v1\\\",\\\"apiKey\\\":\\\"no-key\\\",\\\"api\\\":\\\"openai-completions\\\",\\\"models\\\":[{\\\"id\\\":\\\"\$MP\\\",\\\"name\\\":\\\"$NAME local brain\\\"}]}\" --strict-json
openclaw agents list --json 2>/dev/null | grep -q '\"$NAME\"' || \
  openclaw agents add $NAME --model \"chalet-local/\$MP\" --workspace ~/.sanctum/agents/$NAME/workspace --non-interactive
IDX=\$(openclaw config get agents.list 2>/dev/null | python3 -c 'import sys,json;print([a.get(\"id\") for a in json.load(sys.stdin)].index(\"$NAME\"))')
openclaw config set \"agents.list[\$IDX].tools.profile\" minimal
openclaw config set \"agents.list[\$IDX].tools.alsoAllow\" '[\"bundle-mcp\"]' --strict-json
openclaw config set \"agents.list[\$IDX].tools.deny\" '[\"exec\",\"read\",\"write\",\"edit\",\"process\"]' --strict-json
openclaw config set \"agents.list[\$IDX].tools.loopDetection\" '{\"enabled\":true,\"historySize\":12,\"warningThreshold\":2,\"criticalThreshold\":3,\"globalCircuitBreakerThreshold\":5}' --strict-json
openclaw config set agents.defaults.timeoutSeconds 240
openclaw config validate | tail -1"
# identity + mandate: prefer the canonical hub mirror if present on the console
if [ -d "$HOME/.sanctum-satellites/chalet/agents/ahsoka/workspace" ]; then
  say "3b. identity from hub mirror"
  scp -q -r "$HOME/.sanctum-satellites/chalet/agents/ahsoka/workspace/"{AGENTS.md,IDENTITY.md,SOUL.md} \
    "$HOST:.sanctum/agents/$NAME/workspace/" && echo "identity files pushed"
  run "export PATH=/opt/homebrew/bin:\$PATH; openclaw agents set-identity --agent $NAME --from-identity --workspace ~/.sanctum/agents/$NAME/workspace >/dev/null && echo identity-synced"
else
  echo "NOTE: no hub mirror found — copy AGENTS.md/IDENTITY.md/SOUL.md into the workspace and run agents set-identity"
fi

say "4. native MCP hands (whitelisted HA verbs + call-cap)"
if [ -f "$HOME/.sanctum-satellites/chalet/bin/ha_mcp.py" ]; then
  scp -q "$HOME/.sanctum-satellites/chalet/bin/ha_mcp.py" "$HOST:.sanctum/bin/ha_mcp.py"
else
  echo "WARN: ha_mcp.py not in mirror — copy it manually"; fi
run "export PATH=/opt/homebrew/bin:\$PATH
[ -d ~/.sanctum/ha-mcp-venv ] || python3.12 -m venv ~/.sanctum/ha-mcp-venv
~/.sanctum/ha-mcp-venv/bin/python -c 'import mcp' 2>/dev/null || ~/.sanctum/ha-mcp-venv/bin/pip install -q -U pip 'mcp[cli]'
openclaw mcp list 2>/dev/null | grep -q chalet-ha || \
  openclaw mcp add chalet-ha --command ~/.sanctum/ha-mcp-venv/bin/python --arg \$HOME/.sanctum/bin/ha_mcp.py --env HA_URL=http://127.0.0.1:8123 --connect-timeout 20
touch ~/.sanctum/ha-map.env
echo mcp-ok"

say "5. smoke test (dry-run — nothing physical moves)"
run "export PATH=/opt/homebrew/bin:\$PATH
HA_DRYRUN=1 openclaw agent --local --agent $NAME --thinking off --timeout 120 --session-id deploy-smoke \
  -m 'Turn on the kitchen lights.' 2>/dev/null | grep -vE '^\[' | head -2"

say "DONE — $NAME deployed on $HOST"
echo "Remaining operator steps: (1) HA long-lived token -> ~/.sanctum/secrets/ha-token (0600);"
echo "(2) map real devices in ~/.sanctum/ha-map.env; (3) run the 48-case eval (ahsoka-eval.py) as the gate."
