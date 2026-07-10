#!/usr/bin/env bash
# Deploy tank to a target host over SSH. Idempotent — safe to re-run.
#
# Required:
#   TANK_DEPLOY_HOST   SSH target, e.g. root@192.0.2.10 (or a Host alias).
#                      Must be able to create users + write to systemd.
# Optional (defaults shown):
#   TANK_SERVICE_USER=tank        unprivileged user the service runs as
#   TANK_INSTALL_DIR=/opt/tank    where source is deployed on the target
#   TANK_SERVICE_HOME=/home/tank  home dir of the service user
#   TANK_PORT=7878                port uvicorn binds
#   TANK_HOUSE_STYLE_URL          if set (e.g. https://design.example.com/latest),
#                                 refresh the vendored house-style assets from
#                                 it at deploy time. Unset → ship the committed
#                                 copy as-is (keeps the repo free of any private
#                                 hostname, and works offline / when forked).
set -euo pipefail

HOST="${TANK_DEPLOY_HOST:?set TANK_DEPLOY_HOST=user@host (the SSH target)}"
SERVICE_USER="${TANK_SERVICE_USER:-tank}"
INSTALL_DIR="${TANK_INSTALL_DIR:-/opt/tank}"
SERVICE_HOME="${TANK_SERVICE_HOME:-/home/$SERVICE_USER}"
PORT="${TANK_PORT:-7878}"
HOUSE_STYLE_URL="${TANK_HOUSE_STYLE_URL:-}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# Env prefix passed to every remote shell so the heredocs can read the config.
ENVS="SERVICE_USER='$SERVICE_USER' INSTALL_DIR='$INSTALL_DIR' SERVICE_HOME='$SERVICE_HOME' PORT='$PORT'"

echo "== Pushing source to $HOST:$INSTALL_DIR =="
ssh "$HOST" "$ENVS bash -s" <<'REMOTE'
set -e
id "$SERVICE_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash -d "$SERVICE_HOME" "$SERVICE_USER"
mkdir -p "$INSTALL_DIR/static/house-style"
REMOTE
scp -q "$SRC_DIR/api.py"            "$HOST:$INSTALL_DIR/api.py"
scp -q "$SRC_DIR/hook.sh"           "$HOST:$INSTALL_DIR/hook.sh"
scp -q "$SRC_DIR/static/index.html" "$HOST:$INSTALL_DIR/static/index.html"
scp -q "$SRC_DIR/static/favicon.svg" "$HOST:$INSTALL_DIR/static/favicon.svg"
scp -q "$SRC_DIR/static/claude-code.png" "$HOST:$INSTALL_DIR/static/claude-code.png"
# Vendored house-style design assets (committed copy = the portable default).
# tokens.css + shell.css + shell.js + logo are also what the "scaffold with
# house-style" new-project option copies into a fresh app, so a generated app
# is self-contained and works offline. The webawesome/ runtime + webawesome-theme.css
# back the <wa-*> controls so the whole kit (incl. Web Awesome) works offline too.
scp -q "$SRC_DIR/static/house-style/tokens.css"          "$HOST:$INSTALL_DIR/static/house-style/tokens.css"
scp -q "$SRC_DIR/static/house-style/shell.css"           "$HOST:$INSTALL_DIR/static/house-style/shell.css"
scp -q "$SRC_DIR/static/house-style/shell.js"            "$HOST:$INSTALL_DIR/static/house-style/shell.js"
scp -q "$SRC_DIR/static/house-style/webawesome-theme.css" "$HOST:$INSTALL_DIR/static/house-style/webawesome-theme.css"
scp -q "$SRC_DIR/static/house-style/logo.png"            "$HOST:$INSTALL_DIR/static/house-style/logo.png"
# The Web Awesome runtime is a directory tree (~2.5 MB, 400+ files); recurse it.
scp -qr "$SRC_DIR/static/house-style/webawesome"         "$HOST:$INSTALL_DIR/static/house-style/"
# Optionally refresh them from the live design system. The URL lives in your
# deploy env, never in the repo; any failure falls back to the vendored copy.
if [ -n "$HOUSE_STYLE_URL" ]; then
  ssh "$HOST" "HOUSE_STYLE_URL='$HOUSE_STYLE_URL' INSTALL_DIR='$INSTALL_DIR' bash -s" <<'REMOTE'
echo "  refreshing house-style from $HOUSE_STYLE_URL"
curl -fsS "$HOUSE_STYLE_URL/tokens.css"          -o "$INSTALL_DIR/static/house-style/tokens.css"          || echo "  (tokens refresh failed; keeping vendored copy)"
curl -fsS "$HOUSE_STYLE_URL/shell.css"           -o "$INSTALL_DIR/static/house-style/shell.css"           || echo "  (shell.css refresh failed; keeping vendored copy)"
curl -fsS "$HOUSE_STYLE_URL/shell.js"            -o "$INSTALL_DIR/static/house-style/shell.js"            || echo "  (shell.js refresh failed; keeping vendored copy)"
curl -fsS "$HOUSE_STYLE_URL/webawesome-theme.css" -o "$INSTALL_DIR/static/house-style/webawesome-theme.css" || echo "  (webawesome-theme refresh failed; keeping vendored copy)"
curl -fsS "$HOUSE_STYLE_URL/assets/logo.png"     -o "$INSTALL_DIR/static/house-style/logo.png"            || echo "  (logo refresh failed; keeping vendored copy)"
# The webawesome/ runtime is a pinned, version-locked tree (~400 files); it is
# not URL-refreshed here (no manifest to walk). The scp above ships the vendored
# copy; bumping Web Awesome means updating that copy in the repo and redeploying.
REMOTE
fi

# Optional host-local config overrides (gitignored). Shipped only if present
# next to this script. Seeds runtime config (git provider, AI endpoint, project
# roots) into a fresh DB without committing private values. Once the DB is
# seeded the DB wins, so this file can be deleted afterwards.
if [ -f "$SRC_DIR/tank.local.env" ]; then
  echo "  shipping tank.local.env"
  scp -q "$SRC_DIR/tank.local.env" "$HOST:$INSTALL_DIR/tank.local.env"
fi

# Render the systemd unit template with the configured user/dir/home/port.
sed -e "s#__SERVICE_USER__#$SERVICE_USER#g" \
    -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" \
    -e "s#__SERVICE_HOME__#$SERVICE_HOME#g" \
    -e "s#__PORT__#$PORT#g" \
    "$SRC_DIR/tank.service" | ssh "$HOST" "cat > /etc/systemd/system/tank.service"

echo "== Setting up venv + deps + ownership =="
ssh "$HOST" "$ENVS bash -s" <<'REMOTE'
set -e
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/hook.sh"
cd "$INSTALL_DIR"
if [ ! -d venv ]; then runuser -u "$SERVICE_USER" -- python3 -m venv venv; fi
runuser -u "$SERVICE_USER" -- ./venv/bin/pip install --quiet --upgrade pip
runuser -u "$SERVICE_USER" -- ./venv/bin/pip install --quiet fastapi uvicorn[standard] sse-starlette pydantic httpx python-multipart
# Project repos may live on a network share — their .git dirs can be owned by
# uids that do not exist on this host, which makes git refuse to operate
# ("dubious ownership"). Allow any path so worktree + push + status all work.
runuser -u "$SERVICE_USER" -- git config --global --add safe.directory "*"
REMOTE

echo "== Ensuring native claude install for the service user =="
ssh "$HOST" "$ENVS bash -s" <<'REMOTE'
set -e
# A root-owned npm install (/usr/bin/claude or /usr/local/bin/claude) cannot
# self-update under the unprivileged service user, so it would silently go
# stale and shadow the native install on PATH. Refuse rather than coexist.
if [ -e /usr/bin/claude ] || [ -e /usr/local/bin/claude ]; then
  echo "ERROR: found a system-wide claude install." >&2
  echo "Remove it first: sudo npm uninstall -g @anthropic-ai/claude-code" >&2
  echo "then re-run deploy.sh." >&2
  exit 1
fi
if ! runuser -u "$SERVICE_USER" -- test -x "$SERVICE_HOME/.local/bin/claude"; then
  echo "  installing claude via native installer for $SERVICE_USER..."
  runuser -u "$SERVICE_USER" -- bash -c "curl -fsSL https://claude.ai/install.sh | bash"
fi
echo -n "  "; runuser -u "$SERVICE_USER" -- "$SERVICE_HOME/.local/bin/claude" --version
REMOTE

echo "== Installing user-level hooks in \$SERVICE_HOME/.claude/settings.json =="
ssh "$HOST" "$ENVS bash -s" <<'REMOTE'
set -e
runuser -u "$SERVICE_USER" -- env INSTALL_DIR="$INSTALL_DIR" SERVICE_HOME="$SERVICE_HOME" python3 - <<'PY'
import json, os, pathlib
home = os.environ["SERVICE_HOME"]
install_dir = os.environ["INSTALL_DIR"]
p = pathlib.Path(home) / ".claude" / "settings.json"
data = {}
if p.exists():
    try: data = json.loads(p.read_text())
    except: data = {}
hook_cmd = {"type": "command", "command": f"{install_dir}/hook.sh"}
def h(kind):
    return [{"hooks": [{**hook_cmd, "args": [kind]}]}]
data["hooks"] = {
    "SessionStart":     h("SessionStart"),
    "UserPromptSubmit": h("UserPromptSubmit"),
    "PreToolUse":       h("PreToolUse"),
    "PostToolUse":      h("PostToolUse"),
    "Stop":             h("Stop"),
    "SessionEnd":       h("SessionEnd"),
}
data.setdefault("permissions", {})["defaultMode"] = "bypassPermissions"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(data, indent=2))
print("user-level settings.json updated:", p)
PY
REMOTE

echo "== Enabling + restarting tank.service =="
ssh "$HOST" 'systemctl daemon-reload && systemctl enable tank.service >/dev/null && systemctl restart tank.service'

echo "== Health check =="
ssh "$HOST" "PORT='$PORT' bash -s" <<'REMOTE'
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf "http://127.0.0.1:$PORT/health"; then echo; exit 0; fi
  sleep 1
done
echo "health endpoint did not come up after 10s" >&2; exit 1
REMOTE
echo "== Done. tank is listening on port $PORT of $HOST =="
