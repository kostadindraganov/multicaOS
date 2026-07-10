#!/usr/bin/env bash
# hook.sh — Claude Code lifecycle hook bridge.
#
# Installed at user level in <service-user-home>/.claude/settings.json. Called
# by Claude Code on every lifecycle event; receives the event JSON on stdin
# and posts it to the tank backend on 127.0.0.1.
#
# task_id is derived from `session_id` in the payload — which equals the
# task_id we passed via `claude --session-id <task-id>`. Hooks for sessions
# tank didn't spawn (e.g. someone running `claude` directly on the host) will
# POST to an unknown task_id and silently fail at the FK layer — that's fine.
#
# Port defaults to 7878 and can be overridden with TANK_PORT.
#
# Usage (from settings.json args[]): hook.sh <event-kind>

set -u
KIND="${1:-unknown}"
PORT="${TANK_PORT:-7878}"
PAYLOAD="$(cat 2>/dev/null || echo '{}')"

# Validate JSON; if claude sent something weird, wrap as raw string.
if ! echo "$PAYLOAD" | jq -e . >/dev/null 2>&1; then
    PAYLOAD=$(jq -nc --arg r "$PAYLOAD" '{raw: $r}')
fi

TASK_ID=$(echo "$PAYLOAD" | jq -r '.session_id // "unknown"')
BODY=$(jq -nc --arg k "$KIND" --argjson p "$PAYLOAD" '{kind: $k, payload: $p}')

# Best-effort POST; never block claude on hook delivery failure.
curl -sfm 5 -X POST "http://127.0.0.1:${PORT}/tasks/${TASK_ID}/events" \
    -H 'Content-Type: application/json' \
    --data-binary "$BODY" >/dev/null 2>&1 || true

exit 0
