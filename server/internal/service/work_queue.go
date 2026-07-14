package service

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/multica-ai/multica/server/internal/events"
	"github.com/multica-ai/multica/server/internal/issueguard"
	"github.com/multica-ai/multica/server/internal/issueposition"
	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
	"github.com/multica-ai/multica/server/pkg/protocol"
)

// workQueueEnqueuer abstracts task enqueueing so WorkQueueService can be
// tested without a full TaskService. Satisfied by *TaskService.
type workQueueEnqueuer interface {
	EnqueueTaskForIssue(ctx context.Context, issue db.Issue, triggerCommentID ...pgtype.UUID) (db.AgentTaskQueue, error)
}

// WorkQueueService drains work_queue items sequentially: at most one item
// per queue runs at a time, and dispatch of the next item is triggered
// either by the scheduler tick (TickAll) or by the prior item terminating
// (SyncItemFromTask).
type WorkQueueService struct {
	Queries   *db.Queries
	TxStarter TxStarter
	Bus       *events.Bus
	TaskSvc   workQueueEnqueuer
}

func NewWorkQueueService(queries *db.Queries, tx TxStarter, bus *events.Bus, taskSvc workQueueEnqueuer) *WorkQueueService {
	return &WorkQueueService{Queries: queries, TxStarter: tx, Bus: bus, TaskSvc: taskSvc}
}

// TickAll advances every runnable work queue by one scheduling step: it
// promotes due `scheduled` queues to `running`, fires due idle-cron queues,
// and then attempts a dispatch on each. Returns the number of items
// dispatched this tick.
func (s *WorkQueueService) TickAll(ctx context.Context, now time.Time) (int, error) {
	queues, err := s.Queries.ListRunnableWorkQueues(ctx)
	if err != nil {
		return 0, fmt.Errorf("list runnable work queues: %w", err)
	}

	dispatched := 0
	for _, queue := range queues {
		switch {
		case queue.Status == "scheduled" && queue.StartAt.Valid && !queue.StartAt.Time.After(now):
			rows, err := s.Queries.MarkWorkQueueScheduledStarted(ctx, queue.ID)
			if err != nil {
				slog.Warn("work queue tick: failed to start scheduled queue", "queue_id", util.UUIDToString(queue.ID), "error", err)
				continue
			}
			if rows == 0 {
				// Another replica already promoted this scheduled queue.
				continue
			}
			queue.Status = "running"
			queue.StartAt = pgtype.Timestamptz{}
			s.publishQueueUpdated(util.UUIDToString(queue.WorkspaceID), util.UUIDToString(queue.ID))

		case queue.Status == "idle" && queue.CronExpression.Valid:
			next, err := NextOccurrenceAfterUTC(queue.CronExpression.String, queueTimezone(queue), now)
			if err != nil {
				slog.Warn("work queue tick: failed to compute next cron occurrence", "queue_id", util.UUIDToString(queue.ID), "error", err)
				continue
			}
			rows, err := s.Queries.MarkWorkQueueCronFired(ctx, db.MarkWorkQueueCronFiredParams{
				ID:        queue.ID,
				NextRunAt: pgtype.Timestamptz{Time: next, Valid: true},
			})
			if err != nil {
				slog.Warn("work queue tick: failed to mark cron fired", "queue_id", util.UUIDToString(queue.ID), "error", err)
				continue
			}
			if rows == 0 {
				// Another replica already fired this cron occurrence.
				continue
			}
			queue.Status = "running"
			queue.NextRunAt = pgtype.Timestamptz{Time: next, Valid: true}
			s.publishQueueUpdated(util.UUIDToString(queue.WorkspaceID), util.UUIDToString(queue.ID))
		}

		didDispatch, err := s.DispatchNext(ctx, queue, now)
		if err != nil {
			slog.Warn("work queue tick: dispatch failed", "queue_id", util.UUIDToString(queue.ID), "error", err)
			continue
		}
		if didDispatch {
			dispatched++
		}
	}
	return dispatched, nil
}

// DispatchNext attempts to start the next pending item on queue. It returns
// (true, nil) when it dispatched (or terminally failed) an item, (false,
// nil) when nothing was dispatched (not running, an item is already in
// flight, the inter-item delay hasn't elapsed yet, start_at is still in the
// future, or the queue drained to idle), and (false, err) on an unexpected
// failure.
func (s *WorkQueueService) DispatchNext(ctx context.Context, queue db.WorkQueue, now time.Time) (bool, error) {
	if queue.Status != "running" {
		return false, nil
	}

	if _, err := s.Queries.GetRunningWorkQueueItem(ctx, queue.ID); err == nil {
		return false, nil
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return false, fmt.Errorf("check running item: %w", err)
	}

	if last, err := s.Queries.LastFinishedWorkQueueItem(ctx, queue.ID); err == nil {
		if last.FinishedAt.Valid {
			delayUntil := last.FinishedAt.Time.Add(time.Duration(queue.ItemDelaySeconds) * time.Second)
			if delayUntil.After(now) {
				return false, nil
			}
		}
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return false, fmt.Errorf("check last finished item: %w", err)
	}

	if queue.StartAt.Valid && queue.StartAt.Time.After(now) {
		return false, nil
	}

	item, err := s.Queries.NextPendingWorkQueueItem(ctx, queue.ID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return s.drainToIdle(ctx, queue, now)
		}
		return false, fmt.Errorf("next pending item: %w", err)
	}

	agentID := queue.DefaultAgentID
	if item.AgentID.Valid {
		agentID = item.AgentID
	}
	if !agentID.Valid {
		if err := s.failItem(ctx, item.ID, "no agent configured"); err != nil {
			return false, err
		}
		s.publishQueueUpdated(util.UUIDToString(queue.WorkspaceID), util.UUIDToString(queue.ID))
		return true, nil
	}

	switch item.Kind {
	case "prompt":
		if err := s.dispatchPrompt(ctx, queue, item, agentID); err != nil {
			return false, err
		}
	case "issue":
		if err := s.dispatchIssueItem(ctx, queue, item, agentID); err != nil {
			return false, err
		}
	default:
		return false, fmt.Errorf("unknown work queue item kind %q", item.Kind)
	}

	s.publishQueueUpdated(util.UUIDToString(queue.WorkspaceID), util.UUIDToString(queue.ID))
	return true, nil
}

// drainToIdle marks a running queue with no pending items as idle, and, for
// cron-backed queues, recomputes and persists the next fire time.
func (s *WorkQueueService) drainToIdle(ctx context.Context, queue db.WorkQueue, now time.Time) (bool, error) {
	if _, err := s.Queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID:          queue.ID,
		WorkspaceID: queue.WorkspaceID,
		Status:      "idle",
		StartAt:     pgtype.Timestamptz{},
	}); err != nil {
		return false, fmt.Errorf("set queue idle: %w", err)
	}

	if queue.CronExpression.Valid && queue.CronExpression.String != "" {
		if queue.RunOnce {
			// run_once: the schedule has served its single fire — clear it so
			// the tick loop never re-fires this queue.
			if _, err := s.Queries.UpdateWorkQueue(ctx, db.UpdateWorkQueueParams{
				ID:          queue.ID,
				WorkspaceID: queue.WorkspaceID,
				SetCron:     true,
			}); err != nil {
				return false, fmt.Errorf("clear run-once schedule: %w", err)
			}
		} else if next, err := NextOccurrenceAfterUTC(queue.CronExpression.String, queueTimezone(queue), now); err != nil {
			slog.Warn("work queue: failed to compute next cron occurrence on drain", "queue_id", util.UUIDToString(queue.ID), "error", err)
		} else if _, err := s.Queries.UpdateWorkQueue(ctx, db.UpdateWorkQueueParams{
			ID:             queue.ID,
			WorkspaceID:    queue.WorkspaceID,
			SetCron:        true,
			CronExpression: queue.CronExpression,
			Timezone:       queue.Timezone,
			NextRunAt:      pgtype.Timestamptz{Time: next, Valid: true},
		}); err != nil {
			return false, fmt.Errorf("update queue next_run_at: %w", err)
		}
	}

	s.publishQueueUpdated(util.UUIDToString(queue.WorkspaceID), util.UUIDToString(queue.ID))
	return false, nil
}

// dispatchPrompt creates an issue from a "prompt" item's title/body,
// assigns it to the resolved agent, and enqueues a task for it. Mirrors
// AutopilotService.dispatchCreateIssue's tx shape and issue:created publish.
func (s *WorkQueueService) dispatchPrompt(ctx context.Context, queue db.WorkQueue, item db.WorkQueueItem, agentID pgtype.UUID) error {
	tx, err := s.TxStarter.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	qtx := s.Queries.WithTx(tx)

	// Guard against re-creating the issue if a prior dispatch attempt
	// crashed between tx.Commit (issue created) and MarkWorkQueueItemRunning
	// (item marked running + linked): the item would still be pending on
	// retry, but the issue would already exist. Mirrors
	// issueguard.LockAndFindRecentAutopilotDuplicate.
	issue, found, err := issueguard.LockAndFindWorkQueueItemIssue(ctx, qtx, item.ID)
	if err != nil {
		return fmt.Errorf("work queue item issue guard: %w", err)
	}

	if !found {
		issueNumber, err := qtx.IncrementIssueCounter(ctx, queue.WorkspaceID)
		if err != nil {
			return fmt.Errorf("increment issue counter: %w", err)
		}

		newPosition, err := issueposition.NextTopPosition(ctx, tx, queue.WorkspaceID, "todo")
		if err != nil {
			return fmt.Errorf("get next issue position: %w", err)
		}

		issue, err = qtx.CreateIssueWithOrigin(ctx, db.CreateIssueWithOriginParams{
			WorkspaceID:   queue.WorkspaceID,
			Title:         item.Title.String,
			Description:   item.Body,
			Status:        "todo",
			Priority:      "none",
			AssigneeType:  pgtype.Text{String: "agent", Valid: true},
			AssigneeID:    agentID,
			CreatorType:   "agent",
			CreatorID:     agentID,
			ParentIssueID: pgtype.UUID{},
			Position:      newPosition,
			StartDate:     pgtype.Date{},
			DueDate:       pgtype.Date{},
			Number:        issueNumber,
			ProjectID:     pgtype.UUID{},
			OriginType:    pgtype.Text{String: "work_queue", Valid: true},
			OriginID:      item.ID,
		})
		if err != nil {
			return fmt.Errorf("create issue: %w", err)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit tx: %w", err)
	}

	if !found {
		prefix := s.getIssuePrefix(queue.WorkspaceID)
		s.Bus.Publish(events.Event{
			Type:        protocol.EventIssueCreated,
			WorkspaceID: util.UUIDToString(queue.WorkspaceID),
			ActorType:   "agent",
			ActorID:     util.UUIDToString(agentID),
			Payload: map[string]any{
				"issue": issueToMap(issue, prefix),
			},
		})
	}

	task, err := s.TaskSvc.EnqueueTaskForIssue(ctx, issue)
	if err != nil {
		return fmt.Errorf("enqueue task for issue: %w", err)
	}

	if _, err := s.Queries.MarkWorkQueueItemRunning(ctx, db.MarkWorkQueueItemRunningParams{
		ID:      item.ID,
		TaskID:  task.ID,
		IssueID: issue.ID,
	}); err != nil {
		return fmt.Errorf("mark item running: %w", err)
	}
	return nil
}

// dispatchIssueItem drives an "issue" item: it reassigns the linked issue to
// the resolved agent if needed and enqueues a task for it. A vanished issue
// terminally fails the item rather than erroring the tick.
func (s *WorkQueueService) dispatchIssueItem(ctx context.Context, queue db.WorkQueue, item db.WorkQueueItem, agentID pgtype.UUID) error {
	issue, err := s.Queries.GetIssue(ctx, item.IssueID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return s.failItem(ctx, item.ID, "issue no longer exists")
		}
		return fmt.Errorf("get issue: %w", err)
	}

	if issue.AssigneeType.String != "agent" || issue.AssigneeID != agentID {
		issue, err = s.Queries.UpdateIssue(ctx, db.UpdateIssueParams{
			ID:            issue.ID,
			AssigneeType:  pgtype.Text{String: "agent", Valid: true},
			AssigneeID:    agentID,
			StartDate:     issue.StartDate,
			DueDate:       issue.DueDate,
			ParentIssueID: issue.ParentIssueID,
			ProjectID:     issue.ProjectID,
			Stage:         issue.Stage,
		})
		if err != nil {
			return fmt.Errorf("update issue assignee: %w", err)
		}
	}

	task, err := s.TaskSvc.EnqueueTaskForIssue(ctx, issue)
	if err != nil {
		return fmt.Errorf("enqueue task for issue: %w", err)
	}

	if _, err := s.Queries.MarkWorkQueueItemRunning(ctx, db.MarkWorkQueueItemRunningParams{
		ID:      item.ID,
		TaskID:  task.ID,
		IssueID: issue.ID,
	}); err != nil {
		return fmt.Errorf("mark item running: %w", err)
	}
	return nil
}

// failItem transitions an item straight from pending to a terminal failed
// state (via the required running hop, since MarkWorkQueueItemTerminal only
// matches status='running') and records reason as its error.
func (s *WorkQueueService) failItem(ctx context.Context, itemID pgtype.UUID, reason string) error {
	if _, err := s.Queries.MarkWorkQueueItemRunning(ctx, db.MarkWorkQueueItemRunningParams{ID: itemID}); err != nil {
		return fmt.Errorf("mark item running: %w", err)
	}
	if _, err := s.Queries.MarkWorkQueueItemTerminal(ctx, db.MarkWorkQueueItemTerminalParams{
		ID:     itemID,
		Status: "failed",
		Error:  pgtype.Text{String: reason, Valid: true},
	}); err != nil {
		return fmt.Errorf("mark item failed: %w", err)
	}
	return nil
}

// Start transitions a queue into scheduled or running. A startAt in the
// future schedules the queue; otherwise it starts running immediately and
// synchronously attempts a dispatch.
func (s *WorkQueueService) Start(ctx context.Context, queue db.WorkQueue, startAt *time.Time) (db.WorkQueue, error) {
	if startAt != nil && startAt.After(time.Now()) {
		updated, err := s.Queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
			ID:          queue.ID,
			WorkspaceID: queue.WorkspaceID,
			Status:      "scheduled",
			StartAt:     pgtype.Timestamptz{Time: *startAt, Valid: true},
		})
		if err != nil {
			return db.WorkQueue{}, fmt.Errorf("schedule queue: %w", err)
		}
		s.publishQueueUpdated(util.UUIDToString(updated.WorkspaceID), util.UUIDToString(updated.ID))
		return updated, nil
	}

	updated, err := s.Queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID:          queue.ID,
		WorkspaceID: queue.WorkspaceID,
		Status:      "running",
		StartAt:     pgtype.Timestamptz{},
	})
	if err != nil {
		return db.WorkQueue{}, fmt.Errorf("start queue: %w", err)
	}
	s.publishQueueUpdated(util.UUIDToString(updated.WorkspaceID), util.UUIDToString(updated.ID))

	if _, err := s.DispatchNext(ctx, updated, time.Now()); err != nil {
		return updated, fmt.Errorf("dispatch after start: %w", err)
	}
	return updated, nil
}

// Pause stops a running or scheduled queue.
func (s *WorkQueueService) Pause(ctx context.Context, queue db.WorkQueue) (db.WorkQueue, error) {
	if queue.Status != "running" && queue.Status != "scheduled" {
		return db.WorkQueue{}, fmt.Errorf("cannot pause work queue in status %q", queue.Status)
	}
	updated, err := s.Queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID:          queue.ID,
		WorkspaceID: queue.WorkspaceID,
		Status:      "paused",
		StartAt:     pgtype.Timestamptz{},
	})
	if err != nil {
		return db.WorkQueue{}, fmt.Errorf("pause queue: %w", err)
	}
	s.publishQueueUpdated(util.UUIDToString(updated.WorkspaceID), util.UUIDToString(updated.ID))
	return updated, nil
}

// Resume restarts a paused queue and synchronously attempts a dispatch.
func (s *WorkQueueService) Resume(ctx context.Context, queue db.WorkQueue) (db.WorkQueue, error) {
	if queue.Status != "paused" {
		return db.WorkQueue{}, fmt.Errorf("cannot resume work queue in status %q", queue.Status)
	}
	updated, err := s.Queries.SetWorkQueueStatus(ctx, db.SetWorkQueueStatusParams{
		ID:          queue.ID,
		WorkspaceID: queue.WorkspaceID,
		Status:      "running",
		StartAt:     pgtype.Timestamptz{},
	})
	if err != nil {
		return db.WorkQueue{}, fmt.Errorf("resume queue: %w", err)
	}
	s.publishQueueUpdated(util.UUIDToString(updated.WorkspaceID), util.UUIDToString(updated.ID))

	if _, err := s.DispatchNext(ctx, updated, time.Now()); err != nil {
		return updated, fmt.Errorf("dispatch after resume: %w", err)
	}
	return updated, nil
}

// SyncItemFromTask updates the work_queue_item linked to task once the task
// reaches a terminal status, and, for a zero-delay running queue, immediately
// attempts to dispatch the next item.
func (s *WorkQueueService) SyncItemFromTask(ctx context.Context, task db.AgentTaskQueue) {
	item, err := s.Queries.GetWorkQueueItemByTaskID(ctx, task.ID)
	if err != nil {
		return // not a work-queue task
	}

	var status string
	var itemErr pgtype.Text
	switch task.Status {
	case "completed":
		status = "completed"
	case "failed", "cancelled", "timeout":
		status = "failed"
		itemErr = pgtype.Text{String: task.Status, Valid: true}
	default:
		return // not terminal yet
	}

	updated, err := s.Queries.MarkWorkQueueItemTerminal(ctx, db.MarkWorkQueueItemTerminalParams{
		ID:     item.ID,
		Status: status,
		Error:  itemErr,
	})
	if err != nil {
		slog.Warn("work queue: failed to mark item terminal from task sync",
			"item_id", util.UUIDToString(item.ID), "task_id", util.UUIDToString(task.ID), "error", err)
		return
	}

	queue, err := s.Queries.GetWorkQueue(ctx, updated.QueueID)
	if err != nil {
		slog.Warn("work queue: failed to load queue after item sync", "queue_id", util.UUIDToString(updated.QueueID), "error", err)
		return
	}

	s.publishQueueUpdated(util.UUIDToString(queue.WorkspaceID), util.UUIDToString(queue.ID))

	if queue.Status == "running" && queue.ItemDelaySeconds == 0 {
		if _, err := s.DispatchNext(ctx, queue, time.Now()); err != nil {
			slog.Warn("work queue: dispatch after item sync failed", "queue_id", util.UUIDToString(queue.ID), "error", err)
		}
	}
}

func (s *WorkQueueService) publishQueueUpdated(workspaceID, queueID string) {
	s.Bus.Publish(events.Event{
		Type:        protocol.EventQueueUpdated,
		WorkspaceID: workspaceID,
		ActorType:   "system",
		Payload: map[string]any{
			"queue_id": queueID,
		},
	})
}

func (s *WorkQueueService) getIssuePrefix(workspaceID pgtype.UUID) string {
	ws, err := s.Queries.GetWorkspace(context.Background(), workspaceID)
	if err != nil {
		return ""
	}
	return ws.IssuePrefix
}

// queueTimezone returns queue's configured timezone, falling back to the
// same default Autopilot cron evaluation uses.
func queueTimezone(queue db.WorkQueue) string {
	if queue.Timezone.Valid && queue.Timezone.String != "" {
		return queue.Timezone.String
	}
	return DefaultAutopilotTriggerTimezone
}
