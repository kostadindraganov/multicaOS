package service

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/multica-ai/multica/server/internal/events"
	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// workQueueTestPool returns a pool against the configured DATABASE_URL, or
// skips the test if the database is not reachable. Mirrors the
// integrationPool helper in internal/scheduler/stale_steal_test.go.
func workQueueTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dbURL := os.Getenv("DATABASE_URL")
	if dbURL == "" {
		dbURL = "postgres://multica:multica@localhost:5432/multica?sslmode=disable"
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		t.Skipf("work queue integration tests require Postgres: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		t.Skipf("work queue integration tests require Postgres: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

type workQueueFixture struct {
	WorkspaceID pgtype.UUID
	AgentID     pgtype.UUID
}

// setupWorkQueueFixture inserts a workspace and a minimal agent (columns
// copied from setupHandlerTestFixture in internal/handler/handler_test.go),
// deleting both on test cleanup.
func setupWorkQueueFixture(t *testing.T, pool *pgxpool.Pool) workQueueFixture {
	t.Helper()
	ctx := context.Background()

	var userID, workspaceID string
	if err := pool.QueryRow(ctx, `
		INSERT INTO "user" (name, email) VALUES ($1, $2) RETURNING id
	`, "Work Queue Test User", "workqueue-"+uuid.NewString()+"@test.local").Scan(&userID); err != nil {
		t.Fatalf("insert user: %v", err)
	}

	slug := "wq-test-" + uuid.NewString()[:8]
	if err := pool.QueryRow(ctx, `
		INSERT INTO workspace (name, slug, description, issue_prefix)
		VALUES ($1, $2, $3, $4) RETURNING id
	`, "Work Queue Tests", slug, "temporary workspace for work queue tests", "WQT").Scan(&workspaceID); err != nil {
		t.Fatalf("insert workspace: %v", err)
	}

	if _, err := pool.Exec(ctx, `
		INSERT INTO member (workspace_id, user_id, role) VALUES ($1, $2, 'owner')
	`, workspaceID, userID); err != nil {
		t.Fatalf("insert member: %v", err)
	}

	var runtimeID string
	if err := pool.QueryRow(ctx, `
		INSERT INTO agent_runtime (
			workspace_id, daemon_id, name, runtime_mode, provider, status, device_info, metadata, owner_id, last_seen_at
		)
		VALUES ($1, NULL, $2, 'cloud', $3, 'online', $4, '{}'::jsonb, $5, now())
		RETURNING id
	`, workspaceID, "WQ Test Runtime", "wq_test_runtime", "WQ test runtime", userID).Scan(&runtimeID); err != nil {
		t.Fatalf("insert runtime: %v", err)
	}

	var agentID string
	if err := pool.QueryRow(ctx, `
		INSERT INTO agent (
			workspace_id, name, description, runtime_mode, runtime_config,
			runtime_id, visibility, permission_mode, max_concurrent_tasks, owner_id
		)
		VALUES ($1, $2, '', 'cloud', '{}'::jsonb, $3, 'workspace', 'public_to', 1, $4)
		RETURNING id
	`, workspaceID, "WQ Test Agent", runtimeID, userID).Scan(&agentID); err != nil {
		t.Fatalf("insert agent: %v", err)
	}

	t.Cleanup(func() {
		cleanupCtx := context.Background()
		if _, err := pool.Exec(cleanupCtx, `DELETE FROM workspace WHERE id = $1`, workspaceID); err != nil {
			t.Logf("cleanup workspace: %v", err)
		}
		if _, err := pool.Exec(cleanupCtx, `DELETE FROM "user" WHERE id = $1`, userID); err != nil {
			t.Logf("cleanup user: %v", err)
		}
	})

	wsUUID, err := util.ParseUUID(workspaceID)
	if err != nil {
		t.Fatalf("parse workspace id: %v", err)
	}
	agentUUID, err := util.ParseUUID(agentID)
	if err != nil {
		t.Fatalf("parse agent id: %v", err)
	}
	return workQueueFixture{WorkspaceID: wsUUID, AgentID: agentUUID}
}

// stubEnqueuer records every issue handed to it and returns a fixed task.
type stubEnqueuer struct {
	calls []db.Issue
	next  db.AgentTaskQueue
}

func (s *stubEnqueuer) EnqueueTaskForIssue(ctx context.Context, issue db.Issue, _ ...pgtype.UUID) (db.AgentTaskQueue, error) {
	s.calls = append(s.calls, issue)
	return s.next, nil
}

func newWorkQueueTestService(pool *pgxpool.Pool, enqueuer workQueueEnqueuer) *WorkQueueService {
	queries := db.New(pool)
	bus := events.New()
	return NewWorkQueueService(queries, pool, bus, enqueuer)
}

func createTestWorkQueue(t *testing.T, ctx context.Context, queries *db.Queries, fx workQueueFixture, overrides func(*db.CreateWorkQueueParams)) db.WorkQueue {
	t.Helper()
	params := db.CreateWorkQueueParams{
		WorkspaceID:      fx.WorkspaceID,
		Name:             "Test Queue " + uuid.NewString()[:8],
		Description:      pgtype.Text{},
		DefaultAgentID:   fx.AgentID,
		ItemDelaySeconds: 0,
		CronExpression:   pgtype.Text{},
		Timezone:         pgtype.Text{},
		NextRunAt:        pgtype.Timestamptz{},
		CreatedBy:        pgtype.UUID{},
	}
	if overrides != nil {
		overrides(&params)
	}
	queue, err := queries.CreateWorkQueue(ctx, params)
	if err != nil {
		t.Fatalf("create work queue: %v", err)
	}
	t.Cleanup(func() {
		queries.DeleteWorkQueue(context.Background(), db.DeleteWorkQueueParams{ID: queue.ID, WorkspaceID: queue.WorkspaceID})
	})
	return queue
}

func createTestWorkQueueItem(t *testing.T, ctx context.Context, queries *db.Queries, queue db.WorkQueue, seq int32, overrides func(*db.CreateWorkQueueItemParams)) db.WorkQueueItem {
	t.Helper()
	params := db.CreateWorkQueueItemParams{
		QueueID:     queue.ID,
		WorkspaceID: queue.WorkspaceID,
		Seq:         seq,
		Kind:        "prompt",
		Title:       pgtype.Text{String: "item title", Valid: true},
		Body:        pgtype.Text{String: "item body", Valid: true},
		IssueID:     pgtype.UUID{},
		AgentID:     pgtype.UUID{},
	}
	if overrides != nil {
		overrides(&params)
	}
	item, err := queries.CreateWorkQueueItem(ctx, params)
	if err != nil {
		t.Fatalf("create work queue item: %v", err)
	}
	return item
}

func TestWorkQueueDispatchSequential(t *testing.T) {
	pool := workQueueTestPool(t)
	fx := setupWorkQueueFixture(t, pool)
	ctx := context.Background()
	queries := db.New(pool)

	queue := createTestWorkQueue(t, ctx, queries, fx, func(p *db.CreateWorkQueueParams) {
		p.ItemDelaySeconds = 0
	})
	itemA := createTestWorkQueueItem(t, ctx, queries, queue, 1, func(p *db.CreateWorkQueueItemParams) {
		p.Title = pgtype.Text{String: "item A", Valid: true}
	})
	itemB := createTestWorkQueueItem(t, ctx, queries, queue, 2, func(p *db.CreateWorkQueueItemParams) {
		p.Title = pgtype.Text{String: "item B", Valid: true}
	})

	queue, err := queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID: queue.ID, WorkspaceID: queue.WorkspaceID, Status: "running", StartAt: pgtype.Timestamptz{},
	})
	if err != nil {
		t.Fatalf("set queue running: %v", err)
	}

	taskAID, err := util.ParseUUID(uuid.NewString())
	if err != nil {
		t.Fatalf("parse task id: %v", err)
	}
	enqueuer := &stubEnqueuer{next: db.AgentTaskQueue{ID: taskAID}}
	svc := newWorkQueueTestService(pool, enqueuer)

	dispatched, err := svc.DispatchNext(ctx, queue, time.Now())
	if err != nil {
		t.Fatalf("DispatchNext: %v", err)
	}
	if !dispatched {
		t.Fatalf("expected dispatch to occur")
	}
	if len(enqueuer.calls) != 1 {
		t.Fatalf("expected 1 enqueue call, got %d", len(enqueuer.calls))
	}
	createdIssue := enqueuer.calls[0]
	if !createdIssue.OriginType.Valid || createdIssue.OriginType.String != "work_queue" {
		t.Fatalf("expected origin_type=work_queue, got %+v", createdIssue.OriginType)
	}
	if createdIssue.OriginID != itemA.ID {
		t.Fatalf("expected origin_id=itemA, got %v", createdIssue.OriginID)
	}

	reloadedA, err := queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: itemA.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item A: %v", err)
	}
	if reloadedA.Status != "running" {
		t.Fatalf("expected item A running, got %s", reloadedA.Status)
	}

	// Second dispatch must no-op: item A is still running (sequential guarantee).
	dispatched2, err := svc.DispatchNext(ctx, queue, time.Now())
	if err != nil {
		t.Fatalf("DispatchNext (2nd): %v", err)
	}
	if dispatched2 {
		t.Fatalf("expected no dispatch while item A is running")
	}
	reloadedB, err := queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: itemB.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item B: %v", err)
	}
	if reloadedB.Status != "pending" {
		t.Fatalf("expected item B still pending, got %s", reloadedB.Status)
	}

	// Completing task A should complete item A and immediately dispatch item B (delay=0).
	taskBID, err := util.ParseUUID(uuid.NewString())
	if err != nil {
		t.Fatalf("parse task id: %v", err)
	}
	enqueuer.next = db.AgentTaskQueue{ID: taskBID}
	svc.SyncItemFromTask(ctx, db.AgentTaskQueue{ID: taskAID, Status: "completed"})

	reloadedA, err = queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: itemA.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item A after sync: %v", err)
	}
	if reloadedA.Status != "completed" {
		t.Fatalf("expected item A completed, got %s", reloadedA.Status)
	}
	reloadedB, err = queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: itemB.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item B after sync: %v", err)
	}
	if reloadedB.Status != "running" {
		t.Fatalf("expected item B running after A completes, got %s", reloadedB.Status)
	}
	if len(enqueuer.calls) != 2 {
		t.Fatalf("expected 2 enqueue calls total, got %d", len(enqueuer.calls))
	}
}

func TestWorkQueueDelayBetweenItems(t *testing.T) {
	pool := workQueueTestPool(t)
	fx := setupWorkQueueFixture(t, pool)
	ctx := context.Background()
	queries := db.New(pool)

	queue := createTestWorkQueue(t, ctx, queries, fx, func(p *db.CreateWorkQueueParams) {
		p.ItemDelaySeconds = 3600
	})
	_ = createTestWorkQueueItem(t, ctx, queries, queue, 1, nil)
	itemB := createTestWorkQueueItem(t, ctx, queries, queue, 2, nil)

	queue, err := queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID: queue.ID, WorkspaceID: queue.WorkspaceID, Status: "running", StartAt: pgtype.Timestamptz{},
	})
	if err != nil {
		t.Fatalf("set queue running: %v", err)
	}

	// Simulate item A already finished "now" by creating it directly as a
	// finished item via Mark* calls would require a running item first; use
	// direct SQL to seed a finished item ahead of B for the delay check.
	itemAID, err := util.ParseUUID(uuid.NewString())
	if err != nil {
		t.Fatalf("parse item id: %v", err)
	}
	if _, err := pool.Exec(ctx, `
		INSERT INTO work_queue_item (id, queue_id, workspace_id, seq, kind, title, body, status, finished_at)
		VALUES ($1, $2, $3, 0, 'prompt', 'finished item', 'body', 'completed', now())
	`, itemAID, queue.ID, queue.WorkspaceID); err != nil {
		t.Fatalf("seed finished item: %v", err)
	}

	enqueuer := &stubEnqueuer{}
	svc := newWorkQueueTestService(pool, enqueuer)

	dispatched, err := svc.DispatchNext(ctx, queue, time.Now())
	if err != nil {
		t.Fatalf("DispatchNext: %v", err)
	}
	if dispatched {
		t.Fatalf("expected no dispatch during item delay window")
	}
	reloadedB, err := queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: itemB.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item B: %v", err)
	}
	if reloadedB.Status != "pending" {
		t.Fatalf("expected item B pending during delay, got %s", reloadedB.Status)
	}
	if len(enqueuer.calls) != 0 {
		t.Fatalf("expected no enqueue calls during delay, got %d", len(enqueuer.calls))
	}
}

func TestWorkQueueFailureContinues(t *testing.T) {
	pool := workQueueTestPool(t)
	fx := setupWorkQueueFixture(t, pool)
	ctx := context.Background()
	queries := db.New(pool)

	queue := createTestWorkQueue(t, ctx, queries, fx, func(p *db.CreateWorkQueueParams) {
		p.ItemDelaySeconds = 0
	})
	itemA := createTestWorkQueueItem(t, ctx, queries, queue, 1, nil)
	itemB := createTestWorkQueueItem(t, ctx, queries, queue, 2, nil)

	queue, err := queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID: queue.ID, WorkspaceID: queue.WorkspaceID, Status: "running", StartAt: pgtype.Timestamptz{},
	})
	if err != nil {
		t.Fatalf("set queue running: %v", err)
	}

	taskAID, err := util.ParseUUID(uuid.NewString())
	if err != nil {
		t.Fatalf("parse task id: %v", err)
	}
	enqueuer := &stubEnqueuer{next: db.AgentTaskQueue{ID: taskAID}}
	svc := newWorkQueueTestService(pool, enqueuer)

	if _, err := svc.DispatchNext(ctx, queue, time.Now()); err != nil {
		t.Fatalf("DispatchNext: %v", err)
	}

	taskBID, err := util.ParseUUID(uuid.NewString())
	if err != nil {
		t.Fatalf("parse task id: %v", err)
	}
	enqueuer.next = db.AgentTaskQueue{ID: taskBID}
	svc.SyncItemFromTask(ctx, db.AgentTaskQueue{ID: taskAID, Status: "failed"})

	reloadedA, err := queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: itemA.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item A: %v", err)
	}
	if reloadedA.Status != "failed" {
		t.Fatalf("expected item A failed, got %s", reloadedA.Status)
	}
	if !reloadedA.Error.Valid || reloadedA.Error.String != "failed" {
		t.Fatalf("expected item A error='failed', got %+v", reloadedA.Error)
	}

	reloadedB, err := queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: itemB.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item B: %v", err)
	}
	if reloadedB.Status != "running" {
		t.Fatalf("expected item B dispatched after A failed, got %s", reloadedB.Status)
	}
}

func TestWorkQueueScheduledStart(t *testing.T) {
	pool := workQueueTestPool(t)
	fx := setupWorkQueueFixture(t, pool)
	ctx := context.Background()
	queries := db.New(pool)

	queue := createTestWorkQueue(t, ctx, queries, fx, nil)
	item := createTestWorkQueueItem(t, ctx, queries, queue, 1, nil)

	taskID, err := util.ParseUUID(uuid.NewString())
	if err != nil {
		t.Fatalf("parse task id: %v", err)
	}
	enqueuer := &stubEnqueuer{next: db.AgentTaskQueue{ID: taskID}}
	svc := newWorkQueueTestService(pool, enqueuer)

	startAt := time.Now().Add(time.Hour)
	updated, err := svc.Start(ctx, queue, &startAt)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if updated.Status != "scheduled" {
		t.Fatalf("expected status scheduled, got %s", updated.Status)
	}
	if len(enqueuer.calls) != 0 {
		t.Fatalf("expected no dispatch on schedule, got %d calls", len(enqueuer.calls))
	}

	// ListRunnableWorkQueues filters on the database's now(), not a Go-side
	// clock, so simulate start_at having passed by rewriting it into the
	// past directly rather than sleeping out the hour.
	if _, err := queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID: queue.ID, WorkspaceID: queue.WorkspaceID, Status: "scheduled",
		StartAt: pgtype.Timestamptz{Time: time.Now().Add(-time.Minute), Valid: true},
	}); err != nil {
		t.Fatalf("rewrite start_at into the past: %v", err)
	}

	dispatched, err := svc.TickAll(ctx, time.Now())
	if err != nil {
		t.Fatalf("TickAll: %v", err)
	}
	if dispatched != 1 {
		t.Fatalf("expected 1 dispatch after start_at passes, got %d", dispatched)
	}

	reloadedQueue, err := queries.GetWorkQueueInWorkspace(ctx, db.GetWorkQueueInWorkspaceParams{ID: queue.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload queue: %v", err)
	}
	if reloadedQueue.Status != "running" {
		t.Fatalf("expected queue running, got %s", reloadedQueue.Status)
	}
	reloadedItem, err := queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: item.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item: %v", err)
	}
	if reloadedItem.Status != "running" {
		t.Fatalf("expected item running, got %s", reloadedItem.Status)
	}
}

func TestWorkQueueNoAgentFailsItem(t *testing.T) {
	pool := workQueueTestPool(t)
	fx := setupWorkQueueFixture(t, pool)
	ctx := context.Background()
	queries := db.New(pool)

	queue := createTestWorkQueue(t, ctx, queries, fx, func(p *db.CreateWorkQueueParams) {
		p.DefaultAgentID = pgtype.UUID{}
	})
	item := createTestWorkQueueItem(t, ctx, queries, queue, 1, func(p *db.CreateWorkQueueItemParams) {
		p.AgentID = pgtype.UUID{}
	})

	queue, err := queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID: queue.ID, WorkspaceID: queue.WorkspaceID, Status: "running", StartAt: pgtype.Timestamptz{},
	})
	if err != nil {
		t.Fatalf("set queue running: %v", err)
	}

	enqueuer := &stubEnqueuer{}
	svc := newWorkQueueTestService(pool, enqueuer)

	dispatched, err := svc.DispatchNext(ctx, queue, time.Now())
	if err != nil {
		t.Fatalf("DispatchNext: %v", err)
	}
	if !dispatched {
		t.Fatalf("expected the failed item to count as dispatched")
	}
	if len(enqueuer.calls) != 0 {
		t.Fatalf("expected enqueuer not to be called, got %d calls", len(enqueuer.calls))
	}

	reloadedItem, err := queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: item.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item: %v", err)
	}
	if reloadedItem.Status != "failed" {
		t.Fatalf("expected item failed, got %s", reloadedItem.Status)
	}
	if !reloadedItem.Error.Valid || reloadedItem.Error.String != "no agent configured" {
		t.Fatalf("expected error 'no agent configured', got %+v", reloadedItem.Error)
	}
}

func TestWorkQueueDrainToIdle(t *testing.T) {
	pool := workQueueTestPool(t)
	fx := setupWorkQueueFixture(t, pool)
	ctx := context.Background()
	queries := db.New(pool)

	queue := createTestWorkQueue(t, ctx, queries, fx, nil)
	queue, err := queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID: queue.ID, WorkspaceID: queue.WorkspaceID, Status: "running", StartAt: pgtype.Timestamptz{},
	})
	if err != nil {
		t.Fatalf("set queue running: %v", err)
	}

	enqueuer := &stubEnqueuer{}
	svc := newWorkQueueTestService(pool, enqueuer)

	dispatched, err := svc.DispatchNext(ctx, queue, time.Now())
	if err != nil {
		t.Fatalf("DispatchNext: %v", err)
	}
	if dispatched {
		t.Fatalf("expected no dispatch on an empty queue")
	}

	reloadedQueue, err := queries.GetWorkQueueInWorkspace(ctx, db.GetWorkQueueInWorkspaceParams{ID: queue.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload queue: %v", err)
	}
	if reloadedQueue.Status != "idle" {
		t.Fatalf("expected queue idle, got %s", reloadedQueue.Status)
	}
}

// TestWorkQueueDispatchPromptReusesExistingIssue reproduces the crash window
// between tx.Commit (issue created) and MarkWorkQueueItemRunning (item
// marked running + linked): the item is seeded pending with an issue that
// already carries origin_type=work_queue/origin_id=item.id, simulating a
// prior attempt that crashed before linking. DispatchNext must reuse that
// issue rather than creating a second one.
func TestWorkQueueDispatchPromptReusesExistingIssue(t *testing.T) {
	pool := workQueueTestPool(t)
	fx := setupWorkQueueFixture(t, pool)
	ctx := context.Background()
	queries := db.New(pool)

	queue := createTestWorkQueue(t, ctx, queries, fx, func(p *db.CreateWorkQueueParams) {
		p.ItemDelaySeconds = 0
	})
	item := createTestWorkQueueItem(t, ctx, queries, queue, 1, func(p *db.CreateWorkQueueItemParams) {
		p.Title = pgtype.Text{String: "crash window item", Valid: true}
	})

	issueNumber, err := queries.IncrementIssueCounter(ctx, fx.WorkspaceID)
	if err != nil {
		t.Fatalf("increment issue counter: %v", err)
	}
	preExisting, err := queries.CreateIssueWithOrigin(ctx, db.CreateIssueWithOriginParams{
		WorkspaceID:  fx.WorkspaceID,
		Title:        "crash window item",
		Description:  item.Body,
		Status:       "todo",
		Priority:     "none",
		AssigneeType: pgtype.Text{String: "agent", Valid: true},
		AssigneeID:   fx.AgentID,
		CreatorType:  "agent",
		CreatorID:    fx.AgentID,
		Position:     -1,
		Number:       issueNumber,
		OriginType:   pgtype.Text{String: "work_queue", Valid: true},
		OriginID:     item.ID,
	})
	if err != nil {
		t.Fatalf("seed pre-existing issue: %v", err)
	}

	queue, err = queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID: queue.ID, WorkspaceID: queue.WorkspaceID, Status: "running", StartAt: pgtype.Timestamptz{},
	})
	if err != nil {
		t.Fatalf("set queue running: %v", err)
	}

	taskID, err := util.ParseUUID(uuid.NewString())
	if err != nil {
		t.Fatalf("parse task id: %v", err)
	}
	enqueuer := &stubEnqueuer{next: db.AgentTaskQueue{ID: taskID}}
	svc := newWorkQueueTestService(pool, enqueuer)

	dispatched, err := svc.DispatchNext(ctx, queue, time.Now())
	if err != nil {
		t.Fatalf("DispatchNext: %v", err)
	}
	if !dispatched {
		t.Fatalf("expected dispatch to occur")
	}
	if len(enqueuer.calls) != 1 {
		t.Fatalf("expected 1 enqueue call, got %d", len(enqueuer.calls))
	}
	if enqueuer.calls[0].ID != preExisting.ID {
		t.Fatalf("expected reused issue %v, got %v", preExisting.ID, enqueuer.calls[0].ID)
	}

	reloadedItem, err := queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: item.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item: %v", err)
	}
	if reloadedItem.Status != "running" {
		t.Fatalf("expected item running, got %s", reloadedItem.Status)
	}
	if reloadedItem.IssueID != preExisting.ID {
		t.Fatalf("expected item linked to reused issue %v, got %v", preExisting.ID, reloadedItem.IssueID)
	}

	var issueCount int
	if err := pool.QueryRow(ctx, `
		SELECT count(*) FROM issue WHERE origin_type = 'work_queue' AND origin_id = $1
	`, item.ID).Scan(&issueCount); err != nil {
		t.Fatalf("count issues by origin: %v", err)
	}
	if issueCount != 1 {
		t.Fatalf("expected exactly 1 issue for this work queue item, got %d", issueCount)
	}
}

// TestWorkQueueScheduledStartedGuardsAgainstDoubleDispatch verifies that
// MarkWorkQueueScheduledStarted's status guard makes a second, concurrent
// scheduled->running promotion a no-op, mirroring
// TestWorkQueueCronFireIdempotent's coverage of MarkWorkQueueCronFired.
func TestWorkQueueScheduledStartedGuardsAgainstDoubleDispatch(t *testing.T) {
	pool := workQueueTestPool(t)
	fx := setupWorkQueueFixture(t, pool)
	ctx := context.Background()
	queries := db.New(pool)

	queue := createTestWorkQueue(t, ctx, queries, fx, nil)
	queue, err := queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID: queue.ID, WorkspaceID: queue.WorkspaceID, Status: "scheduled",
		StartAt: pgtype.Timestamptz{Time: time.Now().Add(-time.Minute), Valid: true},
	})
	if err != nil {
		t.Fatalf("set queue scheduled: %v", err)
	}

	rows1, err := queries.MarkWorkQueueScheduledStarted(ctx, queue.ID)
	if err != nil {
		t.Fatalf("MarkWorkQueueScheduledStarted (1st): %v", err)
	}
	if rows1 != 1 {
		t.Fatalf("expected 1 row affected on first promotion, got %d", rows1)
	}

	rows2, err := queries.MarkWorkQueueScheduledStarted(ctx, queue.ID)
	if err != nil {
		t.Fatalf("MarkWorkQueueScheduledStarted (2nd): %v", err)
	}
	if rows2 != 0 {
		t.Fatalf("expected 0 rows affected on repeated scheduled promotion, got %d", rows2)
	}
}

func TestWorkQueueCronFireIdempotent(t *testing.T) {
	pool := workQueueTestPool(t)
	fx := setupWorkQueueFixture(t, pool)
	ctx := context.Background()
	queries := db.New(pool)

	queue := createTestWorkQueue(t, ctx, queries, fx, func(p *db.CreateWorkQueueParams) {
		p.CronExpression = pgtype.Text{String: "0 2 * * *", Valid: true}
		p.Timezone = pgtype.Text{String: "UTC", Valid: true}
		p.NextRunAt = pgtype.Timestamptz{Time: time.Now().Add(-time.Hour), Valid: true}
	})
	item := createTestWorkQueueItem(t, ctx, queries, queue, 1, nil)

	taskID, err := util.ParseUUID(uuid.NewString())
	if err != nil {
		t.Fatalf("parse task id: %v", err)
	}
	enqueuer := &stubEnqueuer{next: db.AgentTaskQueue{ID: taskID}}
	svc := newWorkQueueTestService(pool, enqueuer)

	dispatched, err := svc.TickAll(ctx, time.Now())
	if err != nil {
		t.Fatalf("TickAll: %v", err)
	}
	if dispatched != 1 {
		t.Fatalf("expected 1 dispatch on cron fire, got %d", dispatched)
	}

	reloadedQueue, err := queries.GetWorkQueueInWorkspace(ctx, db.GetWorkQueueInWorkspaceParams{ID: queue.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload queue: %v", err)
	}
	if reloadedQueue.Status != "running" {
		t.Fatalf("expected queue running after cron fire, got %s", reloadedQueue.Status)
	}
	if !reloadedQueue.NextRunAt.Valid || !reloadedQueue.NextRunAt.Time.After(time.Now()) {
		t.Fatalf("expected next_run_at advanced past now, got %+v", reloadedQueue.NextRunAt)
	}

	reloadedItem, err := queries.GetWorkQueueItemInWorkspace(ctx, db.GetWorkQueueItemInWorkspaceParams{ID: item.ID, WorkspaceID: queue.WorkspaceID})
	if err != nil {
		t.Fatalf("reload item: %v", err)
	}
	if reloadedItem.Status != "running" {
		t.Fatalf("expected item running, got %s", reloadedItem.Status)
	}

	// A second, direct MarkWorkQueueCronFired call against the now-running
	// queue must be a no-op (status guard rejects non-idle rows).
	rows, err := queries.MarkWorkQueueCronFired(ctx, db.MarkWorkQueueCronFiredParams{
		ID:        queue.ID,
		NextRunAt: pgtype.Timestamptz{Time: time.Now().Add(24 * time.Hour), Valid: true},
	})
	if err != nil {
		t.Fatalf("MarkWorkQueueCronFired (2nd): %v", err)
	}
	if rows != 0 {
		t.Fatalf("expected 0 rows affected on repeated cron fire, got %d", rows)
	}
}
