# Work Queues — Design

Date: 2026-07-12
Status: approved (brainstorm with user)

## Purpose

Bring tank-style task queues into Multica: collect work items into an ordered,
workspace-scoped queue and have agents drain them sequentially — starting now,
at a specific datetime, with a configurable delay between items, or on a
recurring cron schedule. Surfaced as a new "Queues" sidebar page.

Tank reference (`otheros/tank/`): per-project queue of `{title, detail, seq,
depends_on}` items, start/pause/resume verbs, a runner loop that drains items
one at a time. Tank has no time-based scheduling; that part is new here.

## Decisions (from brainstorm)

- **Queue item is a union**: a raw *prompt* (title + body; becomes a real
  issue at dispatch time) OR a reference to an *existing issue*.
- **Timing (all in v1)**: start now (manual), start at a specific datetime,
  fixed delay between items, recurring cron (each fire drains the pending
  items).
- **Execution**: strictly sequential within a queue (next item dispatches only
  after the previous reaches a terminal state). Queue has a default agent;
  each item may override with its own agent.
- **On item failure**: mark the item `failed` and continue to the next item
  (items are independent issues). A stop-on-failure policy is v2.
- **UI**: a dedicated workspace-scoped sidebar page "Queues", following the
  autopilots page pattern.

## What is reused (no new machinery)

- **Scheduler**: the DB-backed execution scheduler (`server/internal/scheduler/`,
  30s tick, `sys_cron_executions` distributed lock) gets one new job. No new
  ticker.
- **Cron parsing**: `service/cron.go` (`NextOccurrenceAfterUTC`) for recurring
  queues, same as autopilot triggers.
- **Issue creation + enqueue chain**: prompt items dispatch through
  `CreateIssueWithOrigin` + `EnqueueTaskForIssue`, the same path autopilot
  `create_issue` mode uses. Issue items enqueue directly. `agent_task_queue`
  claiming logic is untouched.
- **Task-completion hook**: item completion syncs from the same task-terminal
  path autopilots use (`SyncRunFromTask` pattern).

## Data model (migration 162)

`work_queue` (name avoids collision with `agent_task_queue`):

| column | notes |
| --- | --- |
| id, workspace_id | UUID; workspace FK, CASCADE |
| name, description | description nullable |
| default_agent_id | FK agent, ON DELETE SET NULL; items may override |
| status | CHECK: `idle`, `scheduled`, `running`, `paused` |
| start_at | TIMESTAMPTZ nullable — one-shot deferred start |
| item_delay_seconds | INT NOT NULL DEFAULT 0 |
| cron_expression, timezone | nullable pair — recurring drain |
| next_run_at | computed display + idempotency guard for cron fires |
| created_by, created_at, updated_at | |

`work_queue_item`:

| column | notes |
| --- | --- |
| id, queue_id, workspace_id | queue FK CASCADE |
| seq | INT — ordering; reorder renumbers |
| kind | CHECK: `prompt`, `issue` |
| title, body | prompt kind (title required, body optional) |
| issue_id | issue kind: the referenced issue; prompt kind: set to the created issue at dispatch |
| agent_id | nullable per-item override; NULL = queue default |
| status | CHECK: `pending`, `running`, `completed`, `failed` (`skipped`/`cancelled` are v2, with item-cancel) |
| task_id | FK agent_task_queue — the dispatched run |
| error | text, nullable |
| started_at, finished_at, created_at, updated_at | |

Terminal items remain as history; a "clear finished" verb deletes them.
Validation: a queue can only start if every pending item resolves to an agent
(item override or queue default).

## Drain engine

New `QueueDispatchJob` registered on the existing scheduler. Per tick, for
each `running` queue in the workspace scope:

1. If any item is `running` → do nothing (sequential guarantee).
2. Compute eligibility: `now >= max(start_at, last finished_at + item_delay_seconds)`.
3. Dispatch the lowest-`seq` `pending` item:
   - `prompt` → create issue (`CreateIssueWithOrigin`, origin_type
     `work_queue`, origin_id = item id) assigned to the resolved agent →
     enqueue task; store created `issue_id` + `task_id` on the item.
   - `issue` → enqueue task for the resolved agent on that issue.
   - Item → `running`, `started_at` stamped.
4. No pending items left → queue → `idle` (and `next_run_at` recomputed when
   cron is set).

Lifecycle transitions:

- **Start now** → status `running`; the handler calls the dispatch routine
  once synchronously so the first item starts without waiting for a tick.
- **Start at datetime** → status `scheduled` with `start_at`; the job flips it
  to `running` when due.
- **Cron fire** → `idle` queue with pending items → `running`. Idempotent via
  plan-time guard (same pattern as `autopilot_run.planned_at`).
- **Pause** → `paused`: no new dispatches; the in-flight item finishes.
  **Resume** → `running`.
- Task terminal sync: task completed → item `completed`; task failed/timeout →
  item `failed` + `error`; either way the next item becomes eligible
  (`finished_at` + delay). With delay 0 the sync path dispatches the next item
  immediately instead of waiting for the tick.

Edge handling: deleting a queue deletes its item rows (CASCADE) but never
touches already-created issues/tasks. Deleting an item that is `running` is
rejected (cancel is v2); `pending` items delete freely. An item whose issue
was deleted out from under it fails with a clear error and the queue continues.

## API (workspace-scoped, chi)

- `GET /queues`, `POST /queues`
- `GET /queues/{id}` (includes items), `PATCH /queues/{id}`, `DELETE /queues/{id}`
- `POST /queues/{id}/items` — batch add `[{kind, title?, body?, issue_id?, agent_id?}]`
- `PATCH /queues/{id}/items/{itemId}`, `DELETE /queues/{id}/items/{itemId}`
- `POST /queues/{id}/items/reorder` — `{order: [itemId...]}`
- Verbs: `POST /queues/{id}/start` (optional `{start_at}`), `/pause`,
  `/resume`, `/clear-finished`

Repo rules honored: zod schemas + `parseWithFallback` for every response
consumed by UI logic, plus a malformed-response test. UUID path params resolve
through a loader before writes. All queries filter by `workspace_id`.

Realtime: a `queue:updated` WebSocket event invalidates the queue queries in
the TanStack Query cache (no server data mirrored into Zustand).

## Frontend

- `packages/core/queues/`: types, `queryOptions` (keys include `wsId`),
  mutations.
- `packages/views/queues/`: `queues-page.tsx` (list: name, agent, status,
  next run, item counts) and `queue-detail-page.tsx` (ordered item list with
  drag reorder; add-item composer with prompt textarea / issue picker and
  per-item agent dropdown; schedule section with start-at datetime, delay,
  cron; Start/Pause/Resume; live status badges).
- Sidebar: new `NavKey "queues"` entry in `workspaceNav`
  (`packages/views/layout/app-sidebar.tsx`), path builder in
  `packages/core/paths/paths.ts`, i18n labels in
  `packages/views/locales/*/layout.json` (follow
  `apps/docs/.../conventions.zh.mdx` glossary for Chinese copy).
- Routes: `apps/web/app/[workspaceSlug]/(dashboard)/queues/page.tsx` +
  `queues/[id]/page.tsx` thin wrappers; desktop session-route wiring per the
  web/desktop feature rules. Shared code uses `useNavigation()` / `<AppLink>`.

## Testing

- Go service: dispatch order, sequential guarantee, deferred start honoring
  `start_at`, delay between items, cron fire idempotency, failure-continue,
  pause/resume, prompt→issue dispatch linkage.
- Go handler: CRUD + verbs + loader/authz.
- TS: `packages/core` query/mutation tests; `packages/views` page tests
  (list, composer, reorder, verbs); malformed-response schema test.

## Out of scope (v2 candidates)

- Stop-queue-on-failure policy; per-item retries.
- Parallel drain with a concurrency cap.
- Item dependencies (`depends_on`) and cross-queue ordering.
- Import from TODO.md (tank's `import-todos`).
- Cancelling a running item mid-flight.
