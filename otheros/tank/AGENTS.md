# tank — agent context

Context for coding agents (Claude Code, etc.) working in this repo. `CLAUDE.md`
is a symlink to this file.

## What this project is

A mission-control dashboard for spawning **interactive Claude Code sessions on
demand** in tmux, as an unprivileged user, with hook-bridged telemetry, a live
xterm.js terminal modal, and per-task sandbox/worktree dirs. Two failure-modes
deliberately avoided: research-preview surfaces (Channels), and headless `-p`
for the interactive workload.

## Where it runs

- Source-of-truth: this repo.
- Deploys over SSH to a host of your choosing as a systemd unit
  (`tank.service`) under a dedicated unprivileged user. `deploy.sh` is the
  install path; `db.sqlite` and runtime dirs only exist on the deployed host.
- Config: bootstrap via env vars, the rest via the DB-backed settings cog. See
  `config.example.env`.

## Stack

Python FastAPI + SQLite + HTMX (no build step). xterm.js for the terminal
modal, `marked` for markdown rendering. All from CDN. Vanilla.

## Files

| File | Purpose |
|------|---------|
| `api.py` | FastAPI app: task/project CRUD, hook receiver, SSE, settings, WebSocket-to-PTY terminal |
| `hook.sh` | Bash one-liner: POSTs Claude Code lifecycle events to the API |
| `static/index.html` | HTMX dashboard SPA |
| `tank.service` | systemd unit template (`__TOKENS__` rendered by `deploy.sh`) |
| `deploy.sh` | Idempotent SSH deploy |
| `config.example.env` | Documented configuration |
| `.forgejo/workflows/deploy.yml` | Push-to-main → SSH deploy → /health smoke test |

## Configuration model

- **Bootstrap (env-only):** `TANK_INSTALL_DIR`, `TANK_PORT`,
  `TANK_SERVICE_USER`. Needed before the DB exists and/or only applied at
  startup. Paths to the service user's home use `Path.home()` — never hardcode
  a username.
- **Runtime (DB-backed, GUI-editable):** seeded on first boot from
  `RUNTIME_CONFIG_DEFAULTS` (each overridable via `TANK_<KEY>` at seed time),
  read through `cfg()` / `cfg_int()` / `cfg_path()` / `cfg_bool()`, edited via
  `GET`/`PATCH /settings`. After first boot the DB value wins. When adding a new
  runtime setting: add it to `RUNTIME_CONFIG_DEFAULTS`, read it via `cfg*`, and
  surface it in the settings modal in `index.html`.

## Conventions specific to tank

- **Non-root user**: the API runs as an unprivileged user. Claude refuses
  `--dangerously-skip-permissions` as root, so this is load-bearing.
- **Prompt rides the CLI arg, via a file**: the prompt is passed as claude's
  positional argument (`claude --session-id <id> … <prompt>` on first turn,
  `--resume <id> … <prompt>` on continue). Claude ingests and submits it once
  it's up, so there's no `tmux send-keys` step. This is what fixed long prompts
  (send-keys truncated them) and multi-line prompts (each newline submitted
  early); it also removed the old wait-for-`SessionStart`-then-send dance and its
  startup race. **The prompt is NOT inlined into the tmux command string** —
  `tmux new-session` caps that at ~16KB (`command too long`, rc=1), which is its
  own truncation cliff. `_spawn_claude` instead writes the prompt to
  `PROMPTS_ROOT/<id>-t<turn>.txt`; the spawned `sh -c` reads it into a var
  (`p=$(cat …)`), `rm`s the file, then `exec claude … "$p"` — so delivery is
  bounded by ARG_MAX (~2MB), the pane *is* claude (terminal attach + the `pkill
  -f "--session-id <id>"` reaper still match), and the file self-deletes. NB:
  use `--resume <id>`, not `--continue` — `--continue` ignores the positional
  prompt (anthropics/claude-code#3180). The `SessionStart` hook still drives the
  task's status, just not prompt delivery.
- **Pre-trusted sandboxes**: each task's cwd gets a
  `projects.<path>.hasTrustDialogAccepted: true` entry in `~/.claude.json` (via
  `trust_project_path`), so claude doesn't prompt the "trust this folder?"
  wizard on first launch.
- **Orphan reapers**: `delete_task` pkills `claude --session-id <id>` AND
  `claude --resume <id>` then sleeps before filesystem cleanup. Without this,
  claude can re-create state after `shutil.rmtree`.
- **Native-installer claude, not npm**: claude lives at
  `~/.local/bin/claude` for the service user (native installer) and
  auto-updates in the background. `deploy.sh` refuses to proceed if it finds a
  root-owned `/usr/bin/claude` or `/usr/local/bin/claude`, which can't
  self-update under the service user and would silently go stale. The systemd
  unit pins `PATH` so spawned subprocesses pick up the right binary.
- **AI features are optional**: title/todo-tidy call an OpenAI-compatible
  endpoint and are gated behind `ai_enabled()`. Off by default — the code
  degrades silently to placeholder titles, so never assume the endpoint exists.
- **Per-project TODO.md**: each project's working dir owns a `TODO.md`
  markdown checklist that tank reads/writes as the source of truth (it commits
  with the repo and renders fine in any markdown viewer). A todo is one
  checkbox line; its optional **details** body is the indented continuation
  lines beneath it (dedented at `TODO_DETAIL_INDENT` = 6 spaces, under the
  `- [ ] ` marker). `parse_todos` returns `{line_idx, indent, done, text,
  details, end_idx}` per todo — `line_idx` is the checkbox line (the handle
  used by every route) and `end_idx` is the last line of the block, so
  update/delete/reorder rewrite the **whole block** (checkbox + detail lines),
  not just one line. Identity is the line index, so it's fragile under external
  edits but pragmatic; the SPA always re-fetches after a mutation. Routes (all
  `include_in_schema=False`): `GET`/`POST`/`PATCH`/`DELETE
  /projects/{id}/todos[/{line_idx}]` plus `POST …/{line_idx}/tidy` (AI rewrite
  of the headline only — never touches details) and `POST …/todos/reorder`
  (`{order: [line_idx,…]}` → blocks rewritten in that order, file header
  preserved, omitted/unknown idxs handled defensively). The UI renders todos as
  drag-reorderable rows with an inline details editor; "→ task" folds the
  details into the spawned task's prompt.
- **Build queue (overnight self-builder)**: a *DB-backed* queue, distinct from
  the markdown `TODO.md` — `queue_items(id, project_id, parent_id, seq, title,
  detail, status, depends_on, result, agent_task_id, …)` plus one
  `queue_runs(project_id, status, branch, worktree_path, current_item, …)` row
  per project. The whole queue runs on ONE dedicated worktree/branch
  (`tank/queue-<proj8>`, made by `ensure_queue_worktree` off origin/base →
  local base → HEAD so remote-less projects work), items run **sequentially**
  (concurrency 1) each continuing on top of the previous item's auto-committed
  work, so a dependency chain ("schema → API → runner") just works. A single
  background `queue_runner_loop` (started in `lifespan`, skipped under
  `PREVIEW_MODE`, nudged immediately by the `queue_wake` event on start/resume,
  else polling every `queue_poll_secs`) drives every project whose run is
  `running`: it checks the in-flight item's spawned task, finalizes it
  (`completed`→`git add -A && commit` + item `done`; `errored`→`failed`;
  `awaiting_input`→`blocked` + kill its tmux to free the shared worktree), then
  pops the next eligible `pending` item via `_next_eligible_item` (lowest seq,
  deps all `done`; a pending item whose dep is `failed`/`blocked` is itself
  marked `blocked`, one tick per level — that's how failure cascades without
  stalling the rest). The spawned item is a normal task in the queue worktree
  cwd (NOT a per-task worktree); its prompt embeds the item detail + a standing
  "web-search to confirm the approach, do NOT ask questions / enter plan mode,
  do NOT commit" instruction + the queue API curl recipes so a running agent can
  PATCH/POST the remaining (pending-only) items — "update the list as it goes".
  Statuses: `pending → running → done | failed | blocked`. Routes (all
  `include_in_schema=False`): `GET`/`POST` (bulk add) `/projects/{id}/queue`,
  `PATCH`/`DELETE …/queue/{item_id}` (pending-only for content edits/delete),
  `POST …/queue/reorder` (`{order:[id,…]}`), `…/queue/import-todos` (seed from
  TODO.md, undone items only; the queue owns its state after), and
  `…/queue/start` (the Go button — make/adopt the worktree, set `running`, wake
  the runner) / `…/queue/pause` / `…/queue/resume` (= start) / `DELETE …/queue`
  (clear: stop, kill in-flight tmux, remove the worktree but KEEP the branch so
  auto-commits survive, delete items). UI: a queue board on the project home
  (reuses `.dot` status palette + `.todo-*` drag primitives), Go/Pause/import/
  clear, per-item "stream →" opens that item's task terminal, polls every 4s
  while a run is live. **Not yet built**: per-item turn cap (each item is a
  single turn today) and any multi-item parallelism.
- **Live dev previews**: a project carries an optional `dev_command` column (a
  shell template, e.g. `uvicorn api:app --host 0.0.0.0 --port $PORT`). The task
  pane shows a "preview" button when it's set; clicking `POST /tasks/{id}/preview`
  allocates a free port from `preview_port_range` (runtime config, default
  `7900-7950`), launches `export PORT=<n>; <dev_command>` from the task's cwd
  (its worktree) in a detached tmux session named `preview-<task8>-p<port>`, and
  the client opens `http://<dashboard-host>:<port>/`. State lives in the
  in-memory `active_previews` dict, rebuilt from surviving tmux sessions on
  startup by `reconcile_previews` (the port is in the session name, so a restart
  re-adopts running previews and reaps orphans of deleted tasks). `DELETE
  /tasks/{id}/preview` and `delete_task` both call `stop_preview`. The `$PORT`
  template expands via `sh -c` because tmux runs the command through it — it must
  be `export PORT=n; …` not a `PORT=n …` prefix, or the prefix-assignment would
  expand to empty. **Previewing tank itself** is recursive: the tank dev_command
  sets `TANK_INSTALL_DIR="$(pwd)"` (so it serves the worktree's `static/` + an
  isolated `db.sqlite`, not prod's) and `TANK_PREVIEW_MODE=1`. The latter is
  load-bearing — a preview shares the user's tmux server, so without it the
  preview's `reconcile_previews` would find the `preview-…` session it runs
  inside, miss it in its empty DB, and reap it (suicide), killing every other
  live preview too. `PREVIEW_MODE` makes `lifespan` skip all reconcile + sweeper
  jobs (they mutate shared tmux/uploads/trust state) and serve read-only.
- **Git provider is pluggable-ish**: PR/merge are gated on `git_provider`
  (`none` | `forgejo`). The work is delegated to claude via prompt templates
  (`PR_PROMPT_TEMPLATE` / `MERGE_PROMPT_TEMPLATE`); `on_stop` scrapes the
  result for the PR URL / `MERGED` sentinel.
- **Flatten any Forgejo repo**: the header has a "flatten repo" button
  (`openFlattenModal`) that lists repos via `GET /forgejo/repos` and downloads a
  chosen repo as a single text file via `GET /forgejo/flatten?repo=owner/name`.
  The API fetches the repo's `.tar.gz` archive from the Forgejo API and
  concatenates every text file with `===== path =====` headers (binary/oversized
  files are noted, not inlined). Needs `forgejo_url` + `forgejo_token` runtime
  config (settings cog); independent of `git_provider`.
- **Background subagents**: Claude Code fires `Stop` when the main agent parks
  itself waiting for `Agent(run_in_background=true)` subagents. The Stop payload
  includes an undocumented `background_tasks: [{id, status, ...}]` field. While
  any entry is `status: "running"`, `on_stop` treats the Stop as interim — saves
  the wrap-up text as the turn's preview result and flips status to
  `background_waiting`, but does NOT finalize the turn or kill tmux. Claude
  auto-resumes the same session_id when subagents return; the next Stop with no
  pending tasks finalizes normally. Stable as of Claude Code 2.1.x.
- **Scaffold with house-style**: the new-project modal has a "scaffold with tank
  UI" checkbox (`scaffold_ui` on `CreateProjectBody`). When ticked AND the dir
  is freshly `git init`-ed (`action == "initialized"` — never on clone/register,
  so it can't clobber real code), `scaffold_house_style_app` drops a runnable
  Creator Magic starter into it: a minimal FastAPI+HTMX `api.py` (+ `/health`,
  `/`, a `/chat` stub the shared `<cm-chat>` POSTs to), `static/index.html` built
  from the app-shell skeleton, and the house-style assets (`tokens.css`,
  `shell.css`, `shell.js`, `logo.png`) **vendored from tank's own
  `static/house-style/`** so the new app is self-contained and offline-safe. It
  registers a self-bootstrapping `dev_command` (venv + uvicorn on `$PORT`) so the
  preview button works immediately, and makes an initial commit so
  `isolate_tasks` worktrees have a base to branch from. The shared
  `<cm-chat>`/`<cm-drawer>` Web Components live in house-style's `shell.js`; tank
  vendors `shell.css`+`shell.js` (alongside the existing `tokens.css`/`logo.png`)
  and `deploy.sh` ships + optionally refreshes all four from
  `$TANK_HOUSE_STYLE_URL`. Structural changes to the chat box / drawer flow to
  every consumer because they're components, not pasted markup.
- **Per-project icon + live dot**: each project carries an optional `icon`
  column (a [Lucide](https://lucide.dev/icons/) slug). `/projects` also returns
  `live_count` + `live_status` (the highest-priority live status among the
  project's tasks: `awaiting_input` > `background_waiting` > `running`). The
  sidebar renders the icon (CSS-mask tinted to the row colour) plus a pulsing
  status dot next to the name when `live_count > 0`. The icon picker
  (`openIconPicker`) searches Lucide's `tags.json` from jsDelivr (`LUCIDE_VER`
  pins the version); choosing one PATCHes `{icon}` (sanitised to the slug
  charset; empty clears it). The active project's icon becomes the browser-tab
  favicon via `applyProjectFavicon`, which fetches + recolours the stroke SVG to
  the accent and falls back to `/static/favicon.svg` when no icon is set.
  When AI features are on, `POST /projects/{id}/suggest-icon` asks the local
  model (`title_model`) for icon keywords and maps them onto a real Lucide slug
  (`_match_lucide_icon` against the cached `tags.json`: exact slug > exact tag >
  slug substring); the picker surfaces this as "✨ suggest with AI", and new
  projects get a background `_auto_suggest_icon` on creation. The keyword prompt
  is few-shot'd and rejects `finish_reason=length` so a truncated reasoning
  preamble never leaks into the keywords (the model ignores `/no_think`).
- **Public API surface + agent doc**: most routes return JSON and the HTMX SPA
  renders client-side, so the HTTP surface doubles as an integrator API. The
  *public contract* (the `{ }` API button) is the ~13 task/project routes tagged
  `tasks`/`projects`/`system`; everything else (uploads, todos, auth, settings,
  preview, forgejo, the hook receiver) carries `include_in_schema=False` so
  `/openapi.json` + `/docs` show only the integrator-facing subset. Public
  routes carry `response_model`s (`TaskRow`/`TaskDetail`/`TaskHandle`/…); the row
  models use `ConfigDict(extra="allow")` — **load-bearing**, since additive DB
  columns (e.g. `prev_status`) the SPA reads would otherwise be stripped by
  response_model serialisation. `GET /llms.txt` is a hand-maintained, model-
  readable guide (the "paste into an AI agent" artifact); keep it in sync with
  route descriptions when the contract changes.
- **API-token auth**: `api_token` runtime config gates the whole HTTP surface
  via the `api_token_guard` HTTP middleware (+ an inline check in the terminal
  WebSocket, which middleware doesn't see). Blank = auth OFF (historical
  LAN-trust default). When set, callers authenticate by `Authorization: Bearer`,
  `X-API-Token`, the `tank_token` cookie, or `?token=` (the only channel a
  browser `EventSource` can use). Exempt even when on: `/health`, `/llms.txt`,
  `/docs`, `/redoc`, `/openapi.json`, `/static/*`, `/` and the `…/events` hook
  receiver (localhost + unguessable task UUID; `hook.sh` carries no token). The
  SPA rides the cookie: it mirrors a localStorage token into the cookie on load,
  prompts once on a same-origin 401, and (on saving the token in settings)
  mirrors it into the cookie so the operator doesn't lock their own session out.

## Deploy + test

- **Tests**: none yet. Don't add a CI test workflow until there's something
  worth testing.
- **Deploy**: `TANK_DEPLOY_HOST=user@host ./deploy.sh`, or push to `main` to run
  the same via the Forgejo workflow (needs `TANK_SSH_TARGET` + `TANK_DEPLOY_KEY`
  secrets). The workflow smoke-tests `/health`.
