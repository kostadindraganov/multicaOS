# tank

Mission-control dashboard for disposable Claude Code sessions.

Spawn an interactive `claude` in a tmux session on a host you control, send it a
prompt, capture results via lifecycle hooks, and kill the tmux on completion.
Conversations persist on disk between turns via `claude --session-id <uuid>` +
`claude --resume <uuid>` — tmux is just the runtime.

> Runs `claude --dangerously-skip-permissions` as an unprivileged user. Point it
> only at hosts and repos you're comfortable letting an agent modify. There is
> no auth on the dashboard itself — keep it on a trusted network or behind your
> own reverse proxy.

## Stack

Python FastAPI + SQLite + HTMX, no build step. xterm.js for the terminal modal,
`marked` for markdown — all from CDN. Vanilla.

## Architecture

```
Browser  ──HTTP──>  FastAPI (uvicorn, runs as the service user)
                        │
                        ├── SQLite (<install-dir>/db.sqlite)
                        │
                        └── tmux new -d -s task-<id>
                              └── claude --session-id|--resume <uuid>
                                    └── hooks  ──HTTP──>  FastAPI /events
```

## Files

| File | Purpose |
|---|---|
| `api.py` | FastAPI backend: task/project CRUD, tmux spawn, hook receiver, SSE, terminal WebSocket, settings |
| `hook.sh` | Bash one-liner: POSTs Claude Code lifecycle events to the API |
| `static/index.html` | HTMX dashboard (vanilla, no build step) |
| `tank.service` | systemd unit template (rendered by `deploy.sh`) |
| `deploy.sh` | Idempotent SSH deploy: push source + venv + hooks + restart |
| `config.example.env` | All configuration options, documented |
| `.forgejo/workflows/deploy.yml` | Push-to-main → SSH deploy → /health smoke test |

## Requirements (on the target host)

- A non-root user to run the service. Claude refuses
  `--dangerously-skip-permissions` as root, so this is load-bearing.
- `tmux`, `git`, `curl`, `jq`, `python3` (with venv).
- The Claude Code CLI, installed for the service user via the native installer
  (`curl -fsSL https://claude.ai/install.sh | bash`). `deploy.sh` installs it if
  missing and refuses to run alongside a root-owned npm install (which can't
  self-update under the service user and would silently go stale).

## Configure

Everything is configurable. Bootstrap settings (install dir, port, service
user) are environment variables; the rest are seeded into the database on first
boot and editable live from the dashboard's ⚙ settings cog. See
`config.example.env` for the full list. Nothing here is secret — Claude
credentials live in `~/.claude/.credentials.json` on the host, set via the
dashboard login flow.

## Deploy

```bash
TANK_DEPLOY_HOST=user@your-host ./deploy.sh
```

Optional overrides: `TANK_SERVICE_USER`, `TANK_INSTALL_DIR`,
`TANK_SERVICE_HOME`, `TANK_PORT`. The script creates the service user if
needed, sets up a venv, installs Claude Code for that user, registers the
lifecycle hooks at user level, renders + installs the systemd unit, and
restarts the service.

Pushing to `main` runs the same deploy via `.forgejo/workflows/deploy.yml` (set
the `TANK_SSH_TARGET` and `TANK_DEPLOY_KEY` secrets first).

## Auth

Done from the web UI. Open the dashboard, click the **claude: not signed in**
pill in the header, click **sign in**, open the URL it gives you in your
browser, paste the code back. The status pill turns green when complete.
Re-auth follows the same path when tokens expire.

CLI fallback (only if the web flow breaks):

```bash
ssh <service-user>@<host>
claude auth login && claude auth status
```

## Use

1. Open the dashboard (`http://<host>:<port>/`, default port 7878).
2. Click **+ new**, pick or create a project, give it a prompt, submit.
3. Watch turns + events stream live; open the **terminal** modal to attach.
4. **send & continue** adds follow-up turns (resumes the same Claude
   conversation — full memory preserved between turns).
5. **stop** force-kills a running task.

## Settings (⚙)

- **Dashboard title** — header + browser tab text.
- **Git provider** — `none` (hides PR/merge buttons) or `forgejo`. Forgejo
  expects the project's `origin` remote to embed an API token; tank then
  delegates commit/push/PR/merge to Claude as normal task turns.
- **Project / worktree / chat roots** — where new project dirs, per-task
  worktrees, and disposable chat cwds are created.
- **Max concurrent tasks** — spawn semaphore size (restart to apply).
- **AI title & todo tidy-up** — off by default. When on, tank calls an
  OpenAI-compatible chat-completions endpoint to auto-name tasks/chats and tidy
  todos. Off → tasks keep their placeholder titles.

## Sandbox + permissions

Per-task `bypassPermissions` is set so Claude won't block on permission prompts.
Claude can still touch anything the service user can — for stricter isolation,
enable per-task worktrees on a project (so concurrent tasks don't stomp each
other) and/or layer in a chroot/unshare. There is no network-level isolation
out of the box.

## Lifecycle event flow

| Event | What we do |
|---|---|
| `SessionStart` | Mark task `running`, deliver the queued prompt |
| `UserPromptSubmit` | Log (visible in the events strip) |
| `PreToolUse` / `PostToolUse` | Log; flip to `awaiting_input` around blocking tools |
| `Stop` | Read transcript JSONL → save last assistant text as the turn result → kill tmux → mark `completed` |
| `SessionEnd` | Log |

The Stop payload includes `transcript_path` pointing at
`~/.claude/projects/<cwd-hash>/<uuid>.jsonl`; we walk it backwards for the last
assistant text block.

## Useful commands on the host

```bash
systemctl status tank
journalctl -u tank -f
sqlite3 <install-dir>/db.sqlite "SELECT id, title, status FROM tasks;"
tmux list-sessions
tmux attach -t task-<short-id>-tN    # detach with Ctrl-b d
```
