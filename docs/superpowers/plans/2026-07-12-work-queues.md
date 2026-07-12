# Work Queues Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tank-style work queues in Multica: ordered prompt/issue items drained sequentially by agents, with start-now / start-at-datetime / delay-between-items / recurring-cron scheduling, surfaced on a new "Queues" sidebar page.

**Architecture:** Two new tables (`work_queue`, `work_queue_item`, migration 162) + a `WorkQueueService` in `server/internal/service/` that dispatches items through the existing issue-creation/enqueue chain, driven by one global 30s-cadence job on the existing DB-backed scheduler plus task-terminal bus listeners. Frontend mirrors the autopilots feature exactly (core package + views + sidebar entry).

**Tech Stack:** Go 1.26 (chi, sqlc/pgx, robfig-cron parsing via `service/cron.go`), PostgreSQL, TypeScript (TanStack Query, zod, dnd-kit already installed), Next.js + Electron wrappers.

**Spec:** `docs/superpowers/specs/2026-07-12-work-queues-design.md` — read it first.

## Global Constraints

- All `work_queue*` reads filter by `workspace_id` (`WHERE id = $1 AND workspace_id = $2` loader pattern).
- Item statuses v1: `pending | running | completed | failed`. Queue statuses: `idle | scheduled | running | paused`.
- On item failure the queue CONTINUES to the next item.
- Sequential guarantee: never dispatch while any item of the queue is `running`.
- API JSON consumed by UI lists goes through zod + `parseWithFallback` with an `EMPTY_*` fallback + a malformed-response test.
- i18n: the new `"queues"` nav label must land in ALL four locales (`en`, `ja`, `ko`, `zh-Hans`) in the same commit — `parity.test.ts` enforces it. Chinese copy follows `apps/docs/content/docs/developers/conventions.zh.mdx`.
- Code comments in English. Conventional commits (`feat(queues): ...`).
- Go tests need local Postgres: `DATABASE_URL="postgres://multica:multica@localhost:5432/multica?sslmode=disable"`; run `cd server && go run ./cmd/migrate up` after adding the migration, and `make sqlc` after editing queries.

---

### Task 1: Migration 162 + sqlc queries

**Files:**
- Create: `server/migrations/162_work_queue.up.sql`
- Create: `server/migrations/162_work_queue.down.sql`
- Create: `server/pkg/db/queries/work_queue.sql`

**Interfaces:**
- Produces: generated methods on `*db.Queries`: `CreateWorkQueue`, `GetWorkQueueInWorkspace`, `ListWorkQueues`, `UpdateWorkQueue`, `SetWorkQueueStatus`, `MarkWorkQueueCronFired`, `DeleteWorkQueue`, `CreateWorkQueueItem`, `ListWorkQueueItems`, `GetWorkQueueItemInWorkspace`, `GetWorkQueueItemByTaskID`, `GetRunningWorkQueueItem`, `NextPendingWorkQueueItem`, `LastFinishedWorkQueueItem`, `MaxWorkQueueItemSeq`, `UpdateWorkQueueItem`, `UpdateWorkQueueItemSeq`, `MarkWorkQueueItemRunning`, `MarkWorkQueueItemTerminal`, `DeleteWorkQueueItem`, `DeleteFinishedWorkQueueItems`, `ListRunnableWorkQueues`, plus structs `db.WorkQueue`, `db.WorkQueueItem`.

- [ ] **Step 1: Write the up migration**

`server/migrations/162_work_queue.up.sql`:

```sql
CREATE TABLE work_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    default_agent_id UUID REFERENCES agent(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'idle' CHECK (status IN ('idle', 'scheduled', 'running', 'paused')),
    start_at TIMESTAMPTZ,
    item_delay_seconds INT NOT NULL DEFAULT 0,
    cron_expression TEXT,
    timezone TEXT,
    next_run_at TIMESTAMPTZ,
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_work_queue_workspace ON work_queue(workspace_id);

CREATE TABLE work_queue_item (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_id UUID NOT NULL REFERENCES work_queue(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    seq INT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('prompt', 'issue')),
    title TEXT,
    body TEXT,
    issue_id UUID REFERENCES issue(id) ON DELETE SET NULL,
    agent_id UUID REFERENCES agent(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    task_id UUID,
    error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_work_queue_item_queue ON work_queue_item(queue_id, seq);
CREATE INDEX idx_work_queue_item_task ON work_queue_item(task_id) WHERE task_id IS NOT NULL;
```

- [ ] **Step 2: Write the down migration**

`server/migrations/162_work_queue.down.sql`:

```sql
DROP TABLE IF EXISTS work_queue_item;
DROP TABLE IF EXISTS work_queue;
```

- [ ] **Step 3: Apply migration**

Run: `cd server && go run ./cmd/migrate up`
Expected: applies `162_work_queue` without error. Verify: `psql "$DATABASE_URL" -c '\d work_queue'` shows the table.

- [ ] **Step 4: Write the queries file**

`server/pkg/db/queries/work_queue.sql`:

```sql
-- ============================================================
-- Work queues (tank-style ordered prompt/issue queues)
-- ============================================================

-- name: CreateWorkQueue :one
INSERT INTO work_queue (
    workspace_id, name, description, default_agent_id,
    item_delay_seconds, cron_expression, timezone, next_run_at, created_by
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
RETURNING *;

-- name: GetWorkQueueInWorkspace :one
SELECT * FROM work_queue
WHERE id = $1 AND workspace_id = $2;

-- name: GetWorkQueue :one
SELECT * FROM work_queue
WHERE id = $1;

-- name: ListWorkQueues :many
SELECT * FROM work_queue
WHERE workspace_id = $1
ORDER BY created_at DESC;

-- name: UpdateWorkQueue :one
UPDATE work_queue SET
    name = COALESCE(sqlc.narg('name'), name),
    description = COALESCE(sqlc.narg('description'), description),
    default_agent_id = CASE WHEN sqlc.arg('set_default_agent')::bool THEN sqlc.narg('default_agent_id') ELSE default_agent_id END,
    item_delay_seconds = COALESCE(sqlc.narg('item_delay_seconds'), item_delay_seconds),
    cron_expression = CASE WHEN sqlc.arg('set_cron')::bool THEN sqlc.narg('cron_expression') ELSE cron_expression END,
    timezone = CASE WHEN sqlc.arg('set_cron')::bool THEN sqlc.narg('timezone') ELSE timezone END,
    next_run_at = CASE WHEN sqlc.arg('set_cron')::bool THEN sqlc.narg('next_run_at') ELSE next_run_at END,
    updated_at = now()
WHERE id = $1 AND workspace_id = $2
RETURNING *;

-- name: SetWorkQueueStatus :one
UPDATE work_queue SET status = $3, start_at = sqlc.narg('start_at'), updated_at = now()
WHERE id = $1 AND workspace_id = $2
RETURNING *;

-- MarkWorkQueueCronFired flips an idle cron queue to running exactly once per
-- fire: the status guard makes concurrent tick replicas no-op.
-- name: MarkWorkQueueCronFired :execrows
UPDATE work_queue SET status = 'running', next_run_at = $2, updated_at = now()
WHERE id = $1 AND status = 'idle';

-- name: DeleteWorkQueue :exec
DELETE FROM work_queue
WHERE id = $1 AND workspace_id = $2;

-- name: CreateWorkQueueItem :one
INSERT INTO work_queue_item (
    queue_id, workspace_id, seq, kind, title, body, issue_id, agent_id
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING *;

-- name: ListWorkQueueItems :many
SELECT * FROM work_queue_item
WHERE queue_id = $1 AND workspace_id = $2
ORDER BY seq ASC;

-- name: GetWorkQueueItemInWorkspace :one
SELECT * FROM work_queue_item
WHERE id = $1 AND workspace_id = $2;

-- name: GetWorkQueueItemByTaskID :one
SELECT * FROM work_queue_item
WHERE task_id = $1;

-- name: GetRunningWorkQueueItem :one
SELECT * FROM work_queue_item
WHERE queue_id = $1 AND status = 'running'
LIMIT 1;

-- name: NextPendingWorkQueueItem :one
SELECT * FROM work_queue_item
WHERE queue_id = $1 AND status = 'pending'
ORDER BY seq ASC
LIMIT 1;

-- name: LastFinishedWorkQueueItem :one
SELECT * FROM work_queue_item
WHERE queue_id = $1 AND finished_at IS NOT NULL
ORDER BY finished_at DESC
LIMIT 1;

-- name: MaxWorkQueueItemSeq :one
SELECT COALESCE(MAX(seq), 0)::int FROM work_queue_item
WHERE queue_id = $1;

-- name: UpdateWorkQueueItem :one
UPDATE work_queue_item SET
    title = COALESCE(sqlc.narg('title'), title),
    body = COALESCE(sqlc.narg('body'), body),
    agent_id = CASE WHEN sqlc.arg('set_agent')::bool THEN sqlc.narg('agent_id') ELSE agent_id END,
    updated_at = now()
WHERE id = $1 AND workspace_id = $2 AND status = 'pending'
RETURNING *;

-- name: UpdateWorkQueueItemSeq :exec
UPDATE work_queue_item SET seq = $3, updated_at = now()
WHERE id = $1 AND queue_id = $2;

-- name: MarkWorkQueueItemRunning :one
UPDATE work_queue_item SET
    status = 'running', task_id = $2, issue_id = COALESCE(sqlc.narg('issue_id'), issue_id),
    started_at = now(), updated_at = now()
WHERE id = $1
RETURNING *;

-- name: MarkWorkQueueItemTerminal :one
UPDATE work_queue_item SET
    status = $2, error = sqlc.narg('error'), finished_at = now(), updated_at = now()
WHERE id = $1 AND status = 'running'
RETURNING *;

-- name: DeleteWorkQueueItem :execrows
DELETE FROM work_queue_item
WHERE id = $1 AND workspace_id = $2 AND status <> 'running';

-- name: DeleteFinishedWorkQueueItems :execrows
DELETE FROM work_queue_item
WHERE queue_id = $1 AND workspace_id = $2 AND status IN ('completed', 'failed');

-- ListRunnableWorkQueues feeds the global scheduler tick: running queues,
-- scheduled queues whose start_at is due, and idle cron queues whose
-- next_run_at is due and that still hold pending items.
-- name: ListRunnableWorkQueues :many
SELECT * FROM work_queue
WHERE status = 'running'
   OR (status = 'scheduled' AND start_at IS NOT NULL AND start_at <= now())
   OR (
        status = 'idle' AND cron_expression IS NOT NULL
        AND next_run_at IS NOT NULL AND next_run_at <= now()
        AND EXISTS (
            SELECT 1 FROM work_queue_item i
            WHERE i.queue_id = work_queue.id AND i.status = 'pending'
        )
   );
```

- [ ] **Step 5: Regenerate sqlc and build**

Run: `make sqlc && cd server && go build ./...`
Expected: `server/pkg/db/generated/work_queue.sql.go` appears; build passes.

- [ ] **Step 6: Commit**

```bash
git add server/migrations/162_work_queue.up.sql server/migrations/162_work_queue.down.sql server/pkg/db/queries/work_queue.sql server/pkg/db/generated/
git commit -m "feat(queues): work_queue tables + sqlc queries (migration 162)"
```

---

### Task 2: WorkQueueService — drain engine (TDD, DB-backed)

**Files:**
- Create: `server/internal/service/work_queue.go`
- Create: `server/internal/service/work_queue_test.go`
- Modify: `server/pkg/protocol/events.go` (add one constant next to `EventAutopilotRunDone`)

**Interfaces:**
- Consumes: Task 1's `db.Queries` methods; existing `qtx.IncrementIssueCounter`, `issueposition.NextTopPosition(ctx, tx, workspaceID, "todo")`, `qtx.CreateIssueWithOrigin`, `service/cron.go`'s `NextOccurrenceAfterUTC` (check its exact signature in `server/internal/service/cron.go` and adapt the call), `events.Bus`.
- Produces (used by Tasks 3-5):
  - `type WorkQueueService struct { Queries *db.Queries; TxStarter TxStarter; Bus *events.Bus; TaskSvc workQueueEnqueuer }` (`TxStarter` is the same interface `AutopilotService` uses — copy its declaration reference from `autopilot.go`)
  - `func NewWorkQueueService(queries *db.Queries, tx TxStarter, bus *events.Bus, taskSvc workQueueEnqueuer) *WorkQueueService`
  - `type workQueueEnqueuer interface { EnqueueTaskForIssue(ctx context.Context, issue db.Issue, triggerCommentID ...pgtype.UUID) (db.AgentTaskQueue, error) }` (satisfied by `*TaskService`)
  - `func (s *WorkQueueService) TickAll(ctx context.Context, now time.Time) (int, error)`
  - `func (s *WorkQueueService) DispatchNext(ctx context.Context, queue db.WorkQueue, now time.Time) (bool, error)`
  - `func (s *WorkQueueService) Start(ctx context.Context, queue db.WorkQueue, startAt *time.Time) (db.WorkQueue, error)`
  - `func (s *WorkQueueService) Pause(ctx context.Context, queue db.WorkQueue) (db.WorkQueue, error)`
  - `func (s *WorkQueueService) Resume(ctx context.Context, queue db.WorkQueue) (db.WorkQueue, error)`
  - `func (s *WorkQueueService) SyncItemFromTask(ctx context.Context, task db.AgentTaskQueue)`
  - `protocol.EventQueueUpdated = "queue:updated"`

Behavior contract (implement exactly):

1. `DispatchNext(queue)`:
   - If `queue.Status != "running"` → return `(false, nil)`.
   - If `GetRunningWorkQueueItem` finds a row → `(false, nil)` (sequential guarantee).
   - If `LastFinishedWorkQueueItem` exists and `finished_at + item_delay_seconds > now` → `(false, nil)` (delay).
   - If queue has `start_at` in the future → `(false, nil)`.
   - `NextPendingWorkQueueItem`: none → set queue `idle` (and when `cron_expression` set, recompute + persist `next_run_at` via `UpdateWorkQueue` with `set_cron=true`), publish `queue:updated`, return `(false, nil)`.
   - Resolve agent: `item.AgentID` if valid, else `queue.DefaultAgentID`; neither → `MarkWorkQueueItemRunning` then `MarkWorkQueueItemTerminal(status="failed", error="no agent configured")`, publish, return `(true, nil)` (the failed item counts as dispatched; next tick continues).
   - `kind == "prompt"`: inside a tx (mirror `dispatchCreateIssue` in `autopilot.go:288-443`): `IncrementIssueCounter`, `issueposition.NextTopPosition(ctx, tx, wsID, "todo")`, `CreateIssueWithOrigin` with `Status:"todo"`, `Priority:"none"`, `AssigneeType:"agent"`, `AssigneeID:<resolved agent>`, `CreatorType:"agent"`, `CreatorID:<resolved agent>`, `Title:item.Title`, `Description:item.Body`, `OriginType:{String:"work_queue",Valid:true}`, `OriginID:item.ID`; commit; publish `protocol.EventIssueCreated` the same way autopilot.go:392-400 does; then `TaskSvc.EnqueueTaskForIssue(ctx, issue)`; `MarkWorkQueueItemRunning(item.ID, task.ID, issue_id=issue.ID)`.
   - `kind == "issue"`: load the issue (`Queries.GetIssue`); if missing → mark item failed with error `"issue no longer exists"` and return `(true, nil)`; if the issue's assignee differs from the resolved agent, update it with the existing sqlc issue-assignee update query (find it in `server/pkg/db/queries/issue.sql` — the query `UpdateIssue` with assignee params; use the narrowest existing one); then `TaskSvc.EnqueueTaskForIssue`; `MarkWorkQueueItemRunning(item.ID, task.ID, issue_id=<existing>)`.
   - Publish `queue:updated` after any state change. Return `(true, nil)`.
2. `TickAll(now)`: `ListRunnableWorkQueues`; per queue: `scheduled` + due → `SetWorkQueueStatus(running, start_at=nil)`; idle-cron-due → `MarkWorkQueueCronFired(id, nextRun)` where `nextRun` = next occurrence after `now` (skip dispatch if it affected 0 rows — another replica won); then `DispatchNext`. Returns count of dispatches.
3. `Start(queue, startAt)`: `startAt` in the future → `SetWorkQueueStatus("scheduled", start_at=startAt)`; else `SetWorkQueueStatus("running", nil)` then synchronous `DispatchNext`. Both publish.
4. `Pause`: only from `running`/`scheduled` → `paused`. `Resume`: only from `paused` → `running` + `DispatchNext`.
5. `SyncItemFromTask(task)`: `GetWorkQueueItemByTaskID(task.ID)`; not found → return silently. Map `task.Status == "completed"` → `completed`, anything else terminal (`failed`,`cancelled`,`timeout`) → `failed` with `error = task.Status`. `MarkWorkQueueItemTerminal`; load queue; publish; if queue `running` and `item_delay_seconds == 0` → `DispatchNext` immediately.

- [ ] **Step 1: Add the event constant**

In `server/pkg/protocol/events.go`, next to `EventAutopilotRunDone`:

```go
// EventQueueUpdated fires on any work-queue or work-queue-item state change.
EventQueueUpdated = "queue:updated"
```

- [ ] **Step 2: Write failing DB-backed tests**

`server/internal/service/work_queue_test.go`. Copy the `integrationPool(t)` helper shape from `server/internal/scheduler/stale_steal_test.go:19-37` (env `DATABASE_URL`, default `postgres://multica:multica@localhost:5432/multica?sslmode=disable`, `t.Skipf` when unreachable). Add a fixture helper that inserts a workspace, an agent (minimal columns from `agent` table — copy an insert from `server/internal/handler/handler_test.go`'s `setupHandlerTestFixture`), and a `work_queue` + items via the generated queries, with `t.Cleanup` deletes. Stub the enqueuer:

```go
type stubEnqueuer struct {
	calls []db.Issue
	next  db.AgentTaskQueue
}

func (s *stubEnqueuer) EnqueueTaskForIssue(ctx context.Context, issue db.Issue, _ ...pgtype.UUID) (db.AgentTaskQueue, error) {
	s.calls = append(s.calls, issue)
	return s.next, nil
}
```

Tests to write (each creates its own queue fixture):

```go
func TestWorkQueueDispatchSequential(t *testing.T) {
	// queue running, items A(seq1,prompt) B(seq2,prompt), delay 0
	// DispatchNext -> item A running, issue created with origin_type=work_queue, enqueuer called once
	// DispatchNext again -> (false, nil), item B still pending  // sequential guarantee
	// SyncItemFromTask(taskA completed) -> A completed AND B running (delay 0 immediate dispatch)
}

func TestWorkQueueDelayBetweenItems(t *testing.T) {
	// delay 3600s; A finished_at=now; DispatchNext -> false, B stays pending
}

func TestWorkQueueFailureContinues(t *testing.T) {
	// SyncItemFromTask(taskA with Status "failed") -> A failed with error, B dispatched (delay 0)
}

func TestWorkQueueScheduledStart(t *testing.T) {
	// Start(queue, future time) -> status scheduled, nothing dispatched
	// TickAll(now=after start_at) -> status running, first item dispatched
}

func TestWorkQueueNoAgentFailsItem(t *testing.T) {
	// no default_agent_id, item without agent_id -> item failed "no agent configured", enqueuer NOT called
}

func TestWorkQueueDrainToIdle(t *testing.T) {
	// running queue with zero pending items -> DispatchNext flips status to idle
}

func TestWorkQueueCronFireIdempotent(t *testing.T) {
	// idle queue, cron_expression "0 2 * * *", next_run_at in the past, 1 pending item
	// TickAll -> status running, next_run_at advanced past now
	// MarkWorkQueueCronFired again directly -> 0 rows affected (guard)
}
```

Fill in each body with the fixture + assertions (load rows back with the generated queries and assert statuses/fields).

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd server && go test -race ./internal/service/ -run TestWorkQueue`
Expected: FAIL — `undefined: WorkQueueService` etc.

- [ ] **Step 4: Implement `work_queue.go`**

Implement the behavior contract above. Mirror `AutopilotService`'s structure: same `TxStarter` usage (`autopilot.go:294-300`), same `CreateIssueWithOrigin` call shape (`autopilot.go:323-346`), same publish shape (`autopilot.go:392-400`, `1049-1061`). `publishQueueUpdated(wsID, queueID string)` publishes `events.Event{Type: protocol.EventQueueUpdated, WorkspaceID: wsID, ActorType: "system", Payload: map[string]any{"queue_id": queueID}}`. For cron next-occurrence use the existing helper in `server/internal/service/cron.go` (`NextOccurrenceAfterUTC` — check its exact name/signature there before calling).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && go test -race ./internal/service/ -run TestWorkQueue`
Expected: PASS (or SKIP if no local Postgres — start it with `make db-up` first).

- [ ] **Step 6: Commit**

```bash
git add server/internal/service/work_queue.go server/internal/service/work_queue_test.go server/pkg/protocol/events.go
git commit -m "feat(queues): WorkQueueService drain engine with sequential dispatch"
```

---

### Task 3: Scheduler job + task-terminal listeners + wiring

**Files:**
- Create: `server/internal/scheduler/jobs_work_queue.go`
- Create: `server/cmd/server/work_queue_listeners.go`
- Modify: `server/cmd/server/main.go` (~line 418-433, next to the autopilot job registration)

**Interfaces:**
- Consumes: `WorkQueueService.TickAll` / `.SyncItemFromTask` from Task 2; `scheduler.JobSpec` fields (see `jobs_autopilot.go:89-121`); `events.Bus.Subscribe`; `protocol.EventTaskCompleted/Failed/Cancelled`.
- Produces: `scheduler.WorkQueueDispatchJob(ticker WorkQueueTicker) JobSpec` with `JobNameWorkQueueDispatch = "work_queue_dispatch"`; `registerWorkQueueListeners(ctx, bus, svc)`.

- [ ] **Step 1: Write the job**

`server/internal/scheduler/jobs_work_queue.go`:

```go
package scheduler

import (
	"context"
	"time"
)

const JobNameWorkQueueDispatch = "work_queue_dispatch"

// WorkQueueTicker is implemented by service.WorkQueueService; declared here so
// the scheduler package does not import the service package (mirrors
// AutopilotScheduleDispatcher).
type WorkQueueTicker interface {
	TickAll(ctx context.Context, now time.Time) (int, error)
}

// WorkQueueDispatchJob drains runnable work queues on a fixed cadence. All
// per-queue promotion (scheduled/cron) and the sequential-dispatch guarantee
// live in the service; this job is just the periodic heartbeat.
func WorkQueueDispatchJob(ticker WorkQueueTicker) JobSpec {
	return JobSpec{
		Name:              JobNameWorkQueueDispatch,
		Cadence:           30 * time.Second,
		CatchUpMode:       CatchUpLatestOnly,
		RunTimeout:        2 * time.Minute,
		StaleTimeout:      5 * time.Minute,
		HeartbeatInterval: 30 * time.Second,
		AllowStaleReentry: true,
		MaxAttempts:       1,
		Scopes:            StaticScopes(ScopeGlobal),
		Handler: func(ctx context.Context, in HandlerInput) (HandlerResult, error) {
			n, err := ticker.TickAll(ctx, in.PlanTime)
			return HandlerResult{RowsAffected: int64(n)}, err
		},
	}
}
```

(Compare field names against `jobs_autopilot.go:89-121` and `TaskUsageHourlyJob` in `jobs_task_usage.go` — if the cadence job there sets different fields, follow it.)

- [ ] **Step 2: Write the listeners**

`server/cmd/server/work_queue_listeners.go` — mirror `autopilot_listeners.go` exactly:

```go
package main

import (
	"context"

	"github.com/multica-ai/multica/server/internal/events"
	"github.com/multica-ai/multica/server/internal/service"
	"github.com/multica-ai/multica/server/pkg/protocol"
)

// registerWorkQueueListeners keeps work_queue_item rows in sync with the
// terminal state of the agent tasks they dispatched, and advances the queue.
func registerWorkQueueListeners(ctx context.Context, bus *events.Bus, svc *service.WorkQueueService) {
	sync := func(e events.Event) {
		payload, ok := e.Payload.(map[string]any)
		if !ok {
			return
		}
		taskID, ok := payload["task_id"].(string)
		if !ok || taskID == "" {
			return
		}
		task, err := svc.Queries.GetAgentTask(ctx, parseUUID(taskID))
		if err != nil {
			return
		}
		svc.SyncItemFromTask(ctx, task)
	}
	bus.Subscribe(protocol.EventTaskCompleted, sync)
	bus.Subscribe(protocol.EventTaskFailed, sync)
	bus.Subscribe(protocol.EventTaskCancelled, sync)
}
```

(`parseUUID` already exists in the `main` package — `autopilot_listeners.go` uses it. Check the import path module name matches `go.mod`.)

- [ ] **Step 3: Wire in main.go**

In `server/cmd/server/main.go`, where `autopilotSvc` is constructed and the scheduler jobs registered (~:418-433): construct `workQueueSvc := service.NewWorkQueueService(queries, pool, bus, taskSvc)` (pass the same TxStarter/pool value `autopilotSvc` receives — copy its constructor args), then:

```go
if err := schedulerMgr.Register(scheduler.WorkQueueDispatchJob(workQueueSvc)); err != nil {
	slog.Warn("scheduler: failed to register work_queue_dispatch job", "error", err)
}
```

and next to `registerAutopilotListeners(...)`: `registerWorkQueueListeners(sweepCtx, bus, workQueueSvc)` (use the same ctx the autopilot listeners get). Also store `workQueueSvc` where the handler can reach it — see Task 4 Step 1.

- [ ] **Step 4: Build + run scheduler tests**

Run: `cd server && go build ./... && go test -race ./internal/scheduler/ -run WorkQueue`
Expected: build passes (no dedicated scheduler test needed — the job is a thin shim over `TickAll`, already tested in Task 2; if you add one, follow `jobs_autopilot_test.go`).

- [ ] **Step 5: Commit**

```bash
git add server/internal/scheduler/jobs_work_queue.go server/cmd/server/work_queue_listeners.go server/cmd/server/main.go
git commit -m "feat(queues): scheduler dispatch job + task-terminal listeners"
```

---

### Task 4: REST handlers + routes (TDD)

**Files:**
- Create: `server/internal/handler/work_queue.go`
- Create: `server/internal/handler/work_queue_test.go`
- Modify: `server/internal/handler/handler.go` (add `WorkQueueSvc *service.WorkQueueService` field to `Handler`; set it in `New` the same way other services are — inspect how `New` builds/receives services and mirror; if services are attached after `New` in main.go, do that instead)
- Modify: `server/cmd/server/router.go` (inside the `RequireWorkspaceMember` group at ~:976, after the autopilots block at ~:1073)

**Interfaces:**
- Consumes: Task 2 service methods; `parseUUIDOrBadRequest`, `parseUUIDSliceOrBadRequest`, `writeJSON`, `writeError`, `h.resolveWorkspaceID` (all in `handler.go`).
- Produces HTTP API (used by Task 5's client):
  - `GET /api/queues` → `{"queues": [WorkQueue+item_counts], "total": n}`
  - `POST /api/queues` body `{name, description?, default_agent_id?, item_delay_seconds?, cron_expression?, timezone?}` → `{"queue": {...}}`
  - `GET /api/queues/{id}` → `{"queue": {...}, "items": [...]}`
  - `PATCH /api/queues/{id}` (same fields as POST) → `{"queue": {...}}`
  - `DELETE /api/queues/{id}` → 204
  - `POST /api/queues/{id}/items` body `{items: [{kind, title?, body?, issue_id?, agent_id?}]}` → `{"items": [...]}` (validate: `prompt` requires non-empty `title`; `issue` requires `issue_id`; seq assigned from `MaxWorkQueueItemSeq`+1 onward)
  - `PATCH /api/queues/{id}/items/{itemId}` body `{title?, body?, agent_id?}` → `{"item": {...}}` (400 when not pending — the query returns no row)
  - `DELETE /api/queues/{id}/items/{itemId}` → 204, 409 when running (`DeleteWorkQueueItem` returns 0 rows and item exists)
  - `POST /api/queues/{id}/items/reorder` body `{order: ["itemId", ...]}` → 204 (tx: `UpdateWorkQueueItemSeq(id, queueID, i+1)` per element)
  - `POST /api/queues/{id}/start` body `{start_at?: RFC3339}` → `{"queue": {...}}` (calls `Start`)
  - `POST /api/queues/{id}/pause` / `POST /api/queues/{id}/resume` → `{"queue": {...}}`
  - `POST /api/queues/{id}/clear-finished` → `{"deleted": n}`

Every `{id}` resolves through a `loadWorkQueueInWorkspace(w, r, id, workspaceID)` loader (copy `loadAutopilotInWorkspace`, `autopilot.go:502-521`), and `{itemId}` through `GetWorkQueueItemInWorkspace`.

- [ ] **Step 1: Wire the service into Handler**

Add the `WorkQueueSvc` field and set it at construction (mirror however `AutopilotSvc` reaches the handler — grep `AutopilotSvc` in `server/internal/handler/` and `server/cmd/server/main.go` and copy exactly).

- [ ] **Step 2: Write failing handler tests**

`server/internal/handler/work_queue_test.go` uses the existing package harness (`testHandler`, `testPool`, `testWorkspaceID` from `handler_test.go:26-30`). Tests: create → list → add 2 items → reorder → get (assert item order) → start (assert status running via GET) → pause → clear-finished on empty finished set (deleted: 0) → delete queue. Plus one authz test: a queue id from another workspace 404s. Use `httptest.NewRequest` + `chi` route context the same way neighboring handler tests do (copy the request-building helper pattern from `autopilot`-adjacent tests in the package).

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd server && go test -race ./internal/handler/ -run WorkQueue`
Expected: FAIL — undefined handler methods.

- [ ] **Step 4: Implement handlers + mount routes**

`server/internal/handler/work_queue.go` with methods on `*Handler` per the API table above. Mount in `router.go` inside the workspace-member group:

```go
// Work queues
r.Route("/api/queues", func(r chi.Router) {
	r.Get("/", h.ListWorkQueues)
	r.Post("/", h.CreateWorkQueue)
	r.Route("/{id}", func(r chi.Router) {
		r.Get("/", h.GetWorkQueue)
		r.Patch("/", h.UpdateWorkQueue)
		r.Delete("/", h.DeleteWorkQueue)
		r.Post("/start", h.StartWorkQueue)
		r.Post("/pause", h.PauseWorkQueue)
		r.Post("/resume", h.ResumeWorkQueue)
		r.Post("/clear-finished", h.ClearFinishedWorkQueueItems)
		r.Post("/items", h.CreateWorkQueueItems)
		r.Post("/items/reorder", h.ReorderWorkQueueItems)
		r.Route("/items/{itemId}", func(r chi.Router) {
			r.Patch("/", h.UpdateWorkQueueItem)
			r.Delete("/", h.DeleteWorkQueueItem)
		})
	})
})
```

JSON field names in responses come from the sqlc-generated structs (`emit_json_tags: true` — snake_case). For create/patch validate cron with the same helper autopilot trigger validation uses (grep `cron` in `handler/autopilot.go`) and compute initial `next_run_at`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && go test -race ./internal/handler/ -run WorkQueue`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/internal/handler/work_queue.go server/internal/handler/work_queue_test.go server/internal/handler/handler.go server/cmd/server/router.go
git commit -m "feat(queues): REST API for work queues"
```

---

### Task 5: Core TS package — types, schemas, client, queries, mutations, WS

**Files:**
- Create: `packages/core/types/queue.ts` (re-export from `packages/core/types/index.ts` like `autopilot.ts` is)
- Create: `packages/core/queues/queries.ts`, `packages/core/queues/mutations.ts`, `packages/core/queues/index.ts`
- Modify: `packages/core/api/schemas.ts`, `packages/core/api/client.ts`, `packages/core/api/schema.test.ts`, `packages/core/realtime/use-realtime-sync.ts`

**Interfaces:**
- Consumes: Task 4's HTTP API.
- Produces (used by Task 6): `queueKeys.all/list/detail(wsId, ...)`, `queueListOptions(wsId)`, `queueDetailOptions(wsId, id)`, hooks `useCreateQueue`, `useUpdateQueue`, `useDeleteQueue`, `useAddQueueItems`, `useUpdateQueueItem`, `useDeleteQueueItem`, `useReorderQueueItems`, `useStartQueue`, `usePauseQueue`, `useResumeQueue`, `useClearFinishedQueueItems`; types `WorkQueue`, `WorkQueueItem`, `QueueStatus`, `QueueItemKind`, `QueueItemStatus`, `CreateQueueRequest`, `AddQueueItemsRequest`, `ListQueuesResponse`, `GetQueueResponse`.

- [ ] **Step 1: Types**

`packages/core/types/queue.ts`:

```ts
export type QueueStatus = "idle" | "scheduled" | "running" | "paused";
export type QueueItemKind = "prompt" | "issue";
export type QueueItemStatus = "pending" | "running" | "completed" | "failed";

export interface WorkQueue {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  default_agent_id: string | null;
  status: QueueStatus;
  start_at: string | null;
  item_delay_seconds: number;
  cron_expression: string | null;
  timezone: string | null;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkQueueItem {
  id: string;
  queue_id: string;
  seq: number;
  kind: QueueItemKind;
  title: string | null;
  body: string | null;
  issue_id: string | null;
  agent_id: string | null;
  status: QueueItemStatus;
  task_id: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface CreateQueueRequest {
  name: string;
  description?: string;
  default_agent_id?: string;
  item_delay_seconds?: number;
  cron_expression?: string;
  timezone?: string;
}
export type UpdateQueueRequest = Partial<CreateQueueRequest>;

export interface AddQueueItemsRequest {
  items: Array<{
    kind: QueueItemKind;
    title?: string;
    body?: string;
    issue_id?: string;
    agent_id?: string;
  }>;
}

export interface ListQueuesResponse {
  queues: WorkQueue[];
  total: number;
}
export interface GetQueueResponse {
  queue: WorkQueue;
  items: WorkQueueItem[];
}
```

- [ ] **Step 2: zod schemas + failing malformed test**

In `packages/core/api/schemas.ts` (follow the autopilot block at ~:890 — `.loose()`, enum-ish fields as `z.string()`, `.default([])`):

```ts
const WorkQueueSchema = z
  .object({
    id: z.string(),
    workspace_id: z.string(),
    name: z.string(),
    description: z.string().nullable().optional(),
    default_agent_id: z.string().nullable().optional(),
    status: z.string(),
    start_at: z.string().nullable().optional(),
    item_delay_seconds: z.number().default(0),
    cron_expression: z.string().nullable().optional(),
    timezone: z.string().nullable().optional(),
    next_run_at: z.string().nullable().optional(),
    created_at: z.string().optional(),
    updated_at: z.string().optional(),
  })
  .loose();

const WorkQueueItemSchema = z
  .object({
    id: z.string(),
    queue_id: z.string(),
    seq: z.number().default(0),
    kind: z.string(),
    title: z.string().nullable().optional(),
    body: z.string().nullable().optional(),
    issue_id: z.string().nullable().optional(),
    agent_id: z.string().nullable().optional(),
    status: z.string(),
    task_id: z.string().nullable().optional(),
    error: z.string().nullable().optional(),
    started_at: z.string().nullable().optional(),
    finished_at: z.string().nullable().optional(),
  })
  .loose();

export const ListQueuesResponseSchema = z
  .object({ queues: z.array(WorkQueueSchema).default([]), total: z.number().default(0) })
  .loose();
export const EMPTY_LIST_QUEUES_RESPONSE = { queues: [], total: 0 };

export const GetQueueResponseSchema = z
  .object({ queue: WorkQueueSchema, items: z.array(WorkQueueItemSchema).default([]) })
  .loose();
```

In `packages/core/api/schema.test.ts` add a `describe("listQueues")` mirroring `describe("listAutopilots")` (~:112): malformed `{queues: "not-an-array"}` → `{queues: [], total: 0}`; a minimal valid row passes through.

- [ ] **Step 3: Run the schema test — verify it fails**

Run: `cd packages/core && pnpm vitest run schema.test`
Expected: FAIL — `listQueues` not a function.

- [ ] **Step 4: Client methods**

In `packages/core/api/client.ts` (after the autopilots block ~:2142):

```ts
  // Work queues
  async listQueues(): Promise<ListQueuesResponse> {
    const raw = await this.fetch<unknown>(`/api/queues`);
    return parseWithFallback(raw, ListQueuesResponseSchema, EMPTY_LIST_QUEUES_RESPONSE as ListQueuesResponse, {
      endpoint: "GET /api/queues",
    });
  }
  async getQueue(id: string): Promise<GetQueueResponse> {
    const raw = await this.fetch<unknown>(`/api/queues/${id}`);
    return parseWithFallback(raw, GetQueueResponseSchema, { queue: null, items: [] } as unknown as GetQueueResponse, {
      endpoint: "GET /api/queues/:id",
    });
  }
  async createQueue(data: CreateQueueRequest): Promise<{ queue: WorkQueue }> {
    return this.fetch(`/api/queues`, { method: "POST", body: JSON.stringify(data) });
  }
  async updateQueue(id: string, data: UpdateQueueRequest): Promise<{ queue: WorkQueue }> {
    return this.fetch(`/api/queues/${id}`, { method: "PATCH", body: JSON.stringify(data) });
  }
  async deleteQueue(id: string): Promise<void> {
    await this.fetch(`/api/queues/${id}`, { method: "DELETE" });
  }
  async addQueueItems(id: string, data: AddQueueItemsRequest): Promise<{ items: WorkQueueItem[] }> {
    return this.fetch(`/api/queues/${id}/items`, { method: "POST", body: JSON.stringify(data) });
  }
  async updateQueueItem(id: string, itemId: string, data: { title?: string; body?: string; agent_id?: string | null }): Promise<{ item: WorkQueueItem }> {
    return this.fetch(`/api/queues/${id}/items/${itemId}`, { method: "PATCH", body: JSON.stringify(data) });
  }
  async deleteQueueItem(id: string, itemId: string): Promise<void> {
    await this.fetch(`/api/queues/${id}/items/${itemId}`, { method: "DELETE" });
  }
  async reorderQueueItems(id: string, order: string[]): Promise<void> {
    await this.fetch(`/api/queues/${id}/items/reorder`, { method: "POST", body: JSON.stringify({ order }) });
  }
  async startQueue(id: string, startAt?: string): Promise<{ queue: WorkQueue }> {
    return this.fetch(`/api/queues/${id}/start`, { method: "POST", body: JSON.stringify(startAt ? { start_at: startAt } : {}) });
  }
  async pauseQueue(id: string): Promise<{ queue: WorkQueue }> {
    return this.fetch(`/api/queues/${id}/pause`, { method: "POST", body: "{}" });
  }
  async resumeQueue(id: string): Promise<{ queue: WorkQueue }> {
    return this.fetch(`/api/queues/${id}/resume`, { method: "POST", body: "{}" });
  }
  async clearFinishedQueueItems(id: string): Promise<{ deleted: number }> {
    return this.fetch(`/api/queues/${id}/clear-finished`, { method: "POST", body: "{}" });
  }
```

(Match this file's exact `this.fetch` calling convention — inspect a neighboring POST for whether body objects are pre-stringified.)

- [ ] **Step 5: Queries + mutations + WS**

`packages/core/queues/queries.ts` (mirror `autopilots/queries.ts`):

```ts
import { queryOptions } from "@tanstack/react-query";
import { api } from "../api";

export const queueKeys = {
  all: (wsId: string) => ["queues", wsId] as const,
  list: (wsId: string) => [...queueKeys.all(wsId), "list"] as const,
  detail: (wsId: string, id: string) => [...queueKeys.all(wsId), "detail", id] as const,
};

export function queueListOptions(wsId: string) {
  return queryOptions({
    queryKey: queueKeys.list(wsId),
    queryFn: () => api.listQueues(),
    select: (data) => data.queues,
  });
}

export function queueDetailOptions(wsId: string, id: string) {
  return queryOptions({
    queryKey: queueKeys.detail(wsId, id),
    queryFn: () => api.getQueue(id),
  });
}
```

`packages/core/queues/mutations.ts`: one `useMutation` hook per client method, each with `onSettled: () => qc.invalidateQueries({ queryKey: queueKeys.all(wsId) })` (copy the `useTriggerAutopilot` verb-mutation shape from `autopilots/mutations.ts:95` — no optimistic updates). `index.ts` barrel re-exports both files.

`packages/core/realtime/use-realtime-sync.ts`: import `queueKeys` and add to `refreshMap` (~:511):

```ts
      queue: () => {
        const wsId = getCurrentWsId();
        if (wsId) qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
      },
```

- [ ] **Step 6: Run core tests + typecheck**

Run: `cd packages/core && pnpm vitest run schema.test && cd ../.. && pnpm typecheck`
Expected: schema tests PASS; typecheck PASS.

- [ ] **Step 7: Commit**

```bash
git add packages/core/types/queue.ts packages/core/types/index.ts packages/core/queues packages/core/api/schemas.ts packages/core/api/client.ts packages/core/api/schema.test.ts packages/core/realtime/use-realtime-sync.ts
git commit -m "feat(queues): core package — api client, schemas, queries, ws invalidation"
```

---

### Task 6: Views — Queues list + detail pages (TDD)

**Files:**
- Create: `packages/views/queues/components/queues-page.tsx`
- Create: `packages/views/queues/components/queue-detail-page.tsx`
- Create: `packages/views/queues/components/queue-dialog.tsx` (create/edit form)
- Create: `packages/views/queues/components/index.ts`
- Create: `packages/views/queues/components/queues-page.test.tsx`
- Create: i18n namespace files `packages/views/locales/{en,ja,ko,zh-Hans}/queues.json`

**Interfaces:**
- Consumes: Task 5 hooks/options; `useWorkspaceId` from `@multica/core/hooks`; `useWorkspacePaths` from `@multica/core/paths`; `PageHeader` from `../../layout/page-header`; `BreadcrumbHeader` from `../../layout/breadcrumb-header`; `ListGrid*` + `Button`/`Skeleton`/`Dialog`/`Input` from `@multica/ui`; `AgentPicker` from `../../autopilots/components/pickers/agent-picker`; `IssuePickerModal` from `../../modals/issue-picker-modal`; dnd-kit (`DndContext`, `arrayMove` — wiring pattern in `packages/views/issues/components/board-view.tsx:5,15,338,410`); `useT` for i18n.
- Produces: `QueuesPage` (no props), `QueueDetailPage({ queueId }: { queueId: string })` — both exported from `packages/views/queues/components/index.ts`.

Component contract:

- **QueuesPage**: `PageHeader` with title + "New queue" button opening `QueueDialog`; `ListGrid` rows: name, status badge, default agent, item counts (`pending/completed` from a `GET /queues` — counts computed client-side is fine v1 by omitting them; show name/status/next_run_at), row-click navigates to `wsPaths.queueDetail(id)` via `useRowLink()`.
- **QueueDialog** (`mode: "create" | "edit"`): fields name, description, default agent (`AgentPicker`), delay seconds (number input, shown as minutes ×60), optional cron expression + timezone (plain `Input`s v1); submits via `useCreateQueue`/`useUpdateQueue`.
- **QueueDetailPage**: `BreadcrumbHeader`; schedule summary row (status badge, start_at, delay, cron, next_run_at) + verbs: Start (opens a small popover with "now" or datetime-local input → `useStartQueue`), Pause/Resume, Clear finished; item list sorted by `seq`, dnd-kit sortable rows (drag handle; on drop → `arrayMove` + `useReorderQueueItems(order)`); each row shows seq, kind icon, title (or linked issue via `AppLink` to `wsPaths.issueDetail?.(...)` — use the existing issue path builder found in `paths.ts`), agent (`AgentPicker` for pending items), status badge, error text when failed; delete button for non-running items. Add-item composer at the bottom: toggle prompt/issue; prompt → title `Input` + body `Textarea`; issue → button opening `IssuePickerModal`; optional per-item `AgentPicker`; submit via `useAddQueueItems`.

- [ ] **Step 1: Write the failing page test**

`packages/views/queues/components/queues-page.test.tsx` — copy the mock scaffolding from `packages/views/issues/components/issues-page.test.tsx` (`@multica/core/api` mock at :116, `@multica/core/hooks` → `useWorkspaceId: () => "ws-1"`, paths mock at :35, navigation mock at :47). Assert: renders queue names from a mocked `listQueues` response; clicking "New queue" opens the dialog; a queue row shows its status badge.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/views && pnpm vitest run queues-page`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the components + i18n namespace**

Implement per the contract. Create `queues.json` in all four locales with every key used via `useT("queues")` (check how the autopilots namespace file is named/registered — mirror it; keys parity across locales is enforced).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/views && pnpm vitest run queues && cd ../.. && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/views/queues packages/views/locales
git commit -m "feat(queues): queues list and detail views"
```

---

### Task 7: Navigation — sidebar, paths, routes (web + desktop), nav i18n

**Files:**
- Modify: `packages/core/paths/paths.ts` (~:26, after the autopilot entries)
- Modify: `packages/views/layout/app-sidebar.tsx` (`NavKey` union :112, `NavLabelKey` :127, `workspaceNav` :147)
- Modify: `packages/views/locales/{en,ja,ko,zh-Hans}/layout.json` (nav label, same object as `"autopilots"`)
- Create: `apps/web/app/[workspaceSlug]/(dashboard)/queues/page.tsx`
- Create: `apps/web/app/[workspaceSlug]/(dashboard)/queues/[id]/page.tsx`
- Modify: `apps/desktop/src/renderer/src/routes.tsx` (~:141, after the autopilots entries)
- Create: `apps/desktop/src/renderer/src/pages/queue-detail-page.tsx` (desktop wrapper reading `:id`, mirroring `./pages/autopilot-detail-page`)

**Interfaces:**
- Consumes: `QueuesPage` / `QueueDetailPage` from Task 6.

- [ ] **Step 1: Paths + sidebar + labels**

`paths.ts`:

```ts
    queues: () => `${ws}/queues`,
    queueDetail: (id: string) => `${ws}/queues/${encode(id)}`,
```

`app-sidebar.tsx`: add `"queues"` to both `NavKey` and `NavLabelKey`; add `{ key: "queues", labelKey: "queues", icon: ListChecks }` to `workspaceNav` (import `ListChecks` from `lucide-react`). Labels (`layout.json`, all four locales, next to `"autopilots"`): en `"Queues"`, ja `"キュー"`, ko `"대기열"`, zh-Hans `"队列"` (verify zh copy against `conventions.zh.mdx`).

- [ ] **Step 2: Web routes**

`apps/web/app/[workspaceSlug]/(dashboard)/queues/page.tsx`:

```tsx
"use client";

import { QueuesPage } from "@multica/views/queues/components";

export default function Page() {
  return <QueuesPage />;
}
```

`queues/[id]/page.tsx`:

```tsx
"use client";

import { use } from "react";
import { QueueDetailPage } from "@multica/views/queues/components";

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return <QueueDetailPage queueId={id} />;
}
```

- [ ] **Step 3: Desktop routes**

`apps/desktop/src/renderer/src/pages/queue-detail-page.tsx` mirrors `./pages/autopilot-detail-page` (read `:id` from router, render `<QueueDetailPage queueId={id} />`). In `routes.tsx` add:

```tsx
          {
            path: "queues",
            element: <QueuesPage />,
            handle: { title: "Queues" },
          },
          {
            path: "queues/:id",
            element: <DesktopQueueDetailPage />,
            handle: { title: "Queues" },
          },
```

- [ ] **Step 4: Verify**

Run: `pnpm typecheck && cd packages/views && pnpm vitest run` (locale `parity.test.ts` must pass).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/core/paths/paths.ts packages/views/layout/app-sidebar.tsx packages/views/locales apps/web/app apps/desktop/src/renderer/src
git commit -m "feat(queues): sidebar entry, paths, web and desktop routes"
```

---

### Task 8: Full verification + live smoke

- [ ] **Step 1: Full backend tests** — `make db-up` (if needed) then `cd server && go run ./cmd/migrate up && go test -race ./...`. Expected: PASS.
- [ ] **Step 2: Full frontend** — `pnpm typecheck && pnpm test && pnpm lint`. Expected: PASS.
- [ ] **Step 3: Live smoke** — `make dev`; in the web app: create a queue with a default agent, add 2 prompt items + 1 existing issue, hit Start; verify item 1 creates an issue (origin work_queue) and becomes running; complete/fail the task (or stop the daemon to fail it) and verify item 2 dispatches; verify Pause blocks dispatch; set a start_at 2 minutes out and verify the scheduler flips it. Report actual results honestly.
- [ ] **Step 4: Commit any fixes**, then final commit if needed.

---

## Self-Review Notes

- Spec coverage: data model (T1), drain engine incl. all four timing modes (T2+T3), API + zod + malformed test (T4+T5), WS invalidation (T5), UI list/detail/composer/reorder/verbs (T6), sidebar/routes/i18n (T7), tests (throughout), live verification (T8). Spec's "clear finished" verb → T4/T5/T6. Origin type `work_queue` → T2.
- Known judgment calls an implementer may hit: exact `NextOccurrenceAfterUTC` signature (check `service/cron.go`), how `WorkQueueSvc` reaches `*Handler` (mirror `AutopilotSvc`), `this.fetch` body convention in `client.ts`, and the i18n namespace registration for `queues.json` — each task names where to look.
