package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
	"github.com/multica-ai/multica/server/pkg/protocol"
)

// ── Loaders ──────────────────────────────────────────────────────────────────

// loadWorkQueueInWorkspace resolves a client-supplied queue id through a
// workspace-scoped lookup before any write touches it (mirrors
// loadAutopilotInWorkspace). Several work_queue sqlc queries (GetWorkQueue,
// MarkWorkQueueItemRunning/Terminal, NextPendingWorkQueueItem,
// UpdateWorkQueueItemSeq, ...) do not filter by workspace_id themselves —
// handlers must only reach them with an ID that already passed through here.
func (h *Handler) loadWorkQueueInWorkspace(w http.ResponseWriter, r *http.Request, queueID, workspaceID string) (db.WorkQueue, bool) {
	queueUUID, ok := parseUUIDOrBadRequest(w, queueID, "queue id")
	if !ok {
		return db.WorkQueue{}, false
	}
	wsUUID, ok := parseUUIDOrBadRequest(w, workspaceID, "workspace id")
	if !ok {
		return db.WorkQueue{}, false
	}
	queue, err := h.Queries.GetWorkQueueInWorkspace(r.Context(), db.GetWorkQueueInWorkspaceParams{
		ID:          queueUUID,
		WorkspaceID: wsUUID,
	})
	if err != nil {
		writeError(w, http.StatusNotFound, "work queue not found")
		return db.WorkQueue{}, false
	}
	return queue, true
}

// loadWorkQueueItemInWorkspace resolves a client-supplied item id through a
// workspace-scoped lookup. Callers that reach an item via a /queues/{id}/items/{itemId}
// route must additionally check item.QueueID == queue.ID.
func (h *Handler) loadWorkQueueItemInWorkspace(w http.ResponseWriter, r *http.Request, itemID, workspaceID string) (db.WorkQueueItem, bool) {
	itemUUID, ok := parseUUIDOrBadRequest(w, itemID, "item id")
	if !ok {
		return db.WorkQueueItem{}, false
	}
	wsUUID, ok := parseUUIDOrBadRequest(w, workspaceID, "workspace id")
	if !ok {
		return db.WorkQueueItem{}, false
	}
	item, err := h.Queries.GetWorkQueueItemInWorkspace(r.Context(), db.GetWorkQueueItemInWorkspaceParams{
		ID:          itemUUID,
		WorkspaceID: wsUUID,
	})
	if err != nil {
		writeError(w, http.StatusNotFound, "work queue item not found")
		return db.WorkQueueItem{}, false
	}
	return item, true
}

// agentExistsInWorkspace and issueExistsInWorkspace back the FK-existence
// checks below. Client-supplied agent_id / issue_id / default_agent_id are
// FK columns; a well-formed UUID pointing at no row (or a row outside the
// workspace) would otherwise trip a Postgres FK violation on INSERT/UPDATE,
// which the blanket error path maps to an opaque 500. Checking existence
// first turns that into a clear 400, matching validateAutopilotAssignee's
// approach in autopilot.go.
func (h *Handler) agentExistsInWorkspace(r *http.Request, agentID, workspaceID pgtype.UUID) bool {
	_, err := h.Queries.GetAgentInWorkspace(r.Context(), db.GetAgentInWorkspaceParams{
		ID:          agentID,
		WorkspaceID: workspaceID,
	})
	return err == nil
}

func (h *Handler) issueExistsInWorkspace(r *http.Request, issueID, workspaceID pgtype.UUID) bool {
	_, err := h.Queries.GetIssueInWorkspace(r.Context(), db.GetIssueInWorkspaceParams{
		ID:          issueID,
		WorkspaceID: workspaceID,
	})
	return err == nil
}

func (h *Handler) projectExistsInWorkspace(r *http.Request, projectID, workspaceID pgtype.UUID) bool {
	_, err := h.Queries.GetProjectInWorkspace(r.Context(), db.GetProjectInWorkspaceParams{
		ID:          projectID,
		WorkspaceID: workspaceID,
	})
	return err == nil
}

// publishWorkQueueUpdated fires queue:updated for member-triggered writes
// that the WorkQueueService doesn't already cover (create/update/delete on
// the queue itself, and any item mutation).
func (h *Handler) publishWorkQueueUpdated(r *http.Request, workspaceID string, queueID pgtype.UUID) {
	h.publish(protocol.EventQueueUpdated, workspaceID, "member", requestUserID(r), map[string]any{
		"queue_id": uuidToString(queueID),
	})
}

// ── Queue CRUD ───────────────────────────────────────────────────────────────

// workQueueListEntry augments a work_queue row with derived item counts for
// the list view. Embeds db.WorkQueue so its sqlc-generated snake_case json
// tags flatten straight onto the response object.
type workQueueListEntry struct {
	db.WorkQueue
	ItemCounts map[string]int64 `json:"item_counts"`
}

func (h *Handler) ListWorkQueues(w http.ResponseWriter, r *http.Request) {
	workspaceID := h.resolveWorkspaceID(r)
	wsUUID, ok := parseUUIDOrBadRequest(w, workspaceID, "workspace id")
	if !ok {
		return
	}

	queues, err := h.Queries.ListWorkQueues(r.Context(), wsUUID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to list work queues")
		return
	}

	resp := make([]workQueueListEntry, len(queues))
	for i, q := range queues {
		resp[i] = workQueueListEntry{WorkQueue: q.WorkQueue, ItemCounts: map[string]int64{
			"pending":   q.PendingCount,
			"running":   q.RunningCount,
			"completed": q.CompletedCount,
			"failed":    q.FailedCount,
		}}
	}
	writeJSON(w, http.StatusOK, map[string]any{"queues": resp, "total": len(resp)})
}

func (h *Handler) GetWorkQueue(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}

	items, err := h.Queries.ListWorkQueueItems(r.Context(), db.ListWorkQueueItemsParams{
		QueueID:     queue.ID,
		WorkspaceID: queue.WorkspaceID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to load work queue items")
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{"queue": queue, "items": items})
}

type CreateWorkQueueRequest struct {
	Name             string  `json:"name"`
	Description      *string `json:"description"`
	DefaultAgentID   *string `json:"default_agent_id"`
	ProjectID        *string `json:"project_id"`
	ItemDelaySeconds *int32  `json:"item_delay_seconds"`
	CronExpression   *string `json:"cron_expression"`
	Timezone         *string `json:"timezone"`
	RunOnce          *bool   `json:"run_once"`
}

func (h *Handler) CreateWorkQueue(w http.ResponseWriter, r *http.Request) {
	var req CreateWorkQueueRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if req.Name == "" {
		writeError(w, http.StatusBadRequest, "name is required")
		return
	}

	workspaceID := h.resolveWorkspaceID(r)
	wsUUID, ok := parseUUIDOrBadRequest(w, workspaceID, "workspace id")
	if !ok {
		return
	}
	userID, ok := requireUserID(w, r)
	if !ok {
		return
	}
	userUUID, ok := parseUUIDOrBadRequest(w, userID, "user id")
	if !ok {
		return
	}

	var defaultAgentID pgtype.UUID
	if req.DefaultAgentID != nil && *req.DefaultAgentID != "" {
		defaultAgentID, ok = parseUUIDOrBadRequest(w, *req.DefaultAgentID, "default_agent_id")
		if !ok {
			return
		}
		if !h.agentExistsInWorkspace(r, defaultAgentID, wsUUID) {
			writeError(w, http.StatusBadRequest, "default_agent_id must be a valid agent in this workspace")
			return
		}
	}

	var projectID pgtype.UUID
	if req.ProjectID != nil && *req.ProjectID != "" {
		projectID, ok = parseUUIDOrBadRequest(w, *req.ProjectID, "project_id")
		if !ok {
			return
		}
		if !h.projectExistsInWorkspace(r, projectID, wsUUID) {
			writeError(w, http.StatusBadRequest, "project_id must be a valid project in this workspace")
			return
		}
	}

	itemDelay := int32(0)
	if req.ItemDelaySeconds != nil {
		itemDelay = *req.ItemDelaySeconds
	}

	var cronText, tzText pgtype.Text
	var nextRunAt pgtype.Timestamptz
	if req.CronExpression != nil && *req.CronExpression != "" {
		tz := "UTC"
		if req.Timezone != nil && *req.Timezone != "" {
			tz = *req.Timezone
		}
		next, err := computeNextRun(*req.CronExpression, tz)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		cronText = pgtype.Text{String: *req.CronExpression, Valid: true}
		tzText = pgtype.Text{String: tz, Valid: true}
		nextRunAt = pgtype.Timestamptz{Time: next, Valid: true}
	}

	queue, err := h.Queries.CreateWorkQueue(r.Context(), db.CreateWorkQueueParams{
		WorkspaceID:      wsUUID,
		Name:             req.Name,
		Description:      ptrToText(req.Description),
		DefaultAgentID:   defaultAgentID,
		ProjectID:        projectID,
		ItemDelaySeconds: itemDelay,
		CronExpression:   cronText,
		Timezone:         tzText,
		NextRunAt:        nextRunAt,
		CreatedBy:        userUUID,
		RunOnce:          req.RunOnce != nil && *req.RunOnce,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to create work queue")
		return
	}

	h.publishWorkQueueUpdated(r, workspaceID, queue.ID)
	writeJSON(w, http.StatusCreated, map[string]any{"queue": queue})
}

type UpdateWorkQueueRequest struct {
	Name             *string `json:"name"`
	Description      *string `json:"description"`
	DefaultAgentID   *string `json:"default_agent_id"`
	ProjectID        *string `json:"project_id"`
	ItemDelaySeconds *int32  `json:"item_delay_seconds"`
	CronExpression   *string `json:"cron_expression"`
	Timezone         *string `json:"timezone"`
	RunOnce          *bool   `json:"run_once"`
}

func (h *Handler) UpdateWorkQueue(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	workspaceID := h.resolveWorkspaceID(r)

	prev, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}

	bodyBytes, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, http.StatusBadRequest, "failed to read request body")
		return
	}
	var req UpdateWorkQueueRequest
	if err := json.Unmarshal(bodyBytes, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	var rawFields map[string]json.RawMessage
	json.Unmarshal(bodyBytes, &rawFields)

	params := db.UpdateWorkQueueParams{
		ID:          prev.ID,
		WorkspaceID: prev.WorkspaceID,
	}
	if req.Name != nil {
		if *req.Name == "" {
			writeError(w, http.StatusBadRequest, "name cannot be empty")
			return
		}
		params.Name = pgtype.Text{String: *req.Name, Valid: true}
	}
	if _, sent := rawFields["description"]; sent {
		params.Description = ptrToText(req.Description)
	}
	if _, sent := rawFields["item_delay_seconds"]; sent {
		params.ItemDelaySeconds = ptrToInt4(req.ItemDelaySeconds)
	}
	if req.RunOnce != nil {
		params.RunOnce = pgtype.Bool{Bool: *req.RunOnce, Valid: true}
	}
	if _, sent := rawFields["default_agent_id"]; sent {
		params.SetDefaultAgent = true
		if req.DefaultAgentID != nil && *req.DefaultAgentID != "" {
			parsed, pok := parseUUIDOrBadRequest(w, *req.DefaultAgentID, "default_agent_id")
			if !pok {
				return
			}
			if !h.agentExistsInWorkspace(r, parsed, prev.WorkspaceID) {
				writeError(w, http.StatusBadRequest, "default_agent_id must be a valid agent in this workspace")
				return
			}
			params.DefaultAgentID = parsed
		}
	}
	if _, sent := rawFields["project_id"]; sent {
		params.SetProject = true
		if req.ProjectID != nil && *req.ProjectID != "" {
			parsed, pok := parseUUIDOrBadRequest(w, *req.ProjectID, "project_id")
			if !pok {
				return
			}
			if !h.projectExistsInWorkspace(r, parsed, prev.WorkspaceID) {
				writeError(w, http.StatusBadRequest, "project_id must be a valid project in this workspace")
				return
			}
			params.ProjectID = parsed
		}
	}
	_, cronSent := rawFields["cron_expression"]
	_, tzSent := rawFields["timezone"]
	if cronSent || tzSent {
		params.SetCron = true
		newCron := prev.CronExpression
		newTz := prev.Timezone
		if cronSent {
			newCron = ptrToText(req.CronExpression)
		}
		if tzSent {
			newTz = ptrToText(req.Timezone)
		}
		if newCron.Valid && newCron.String != "" {
			tz := "UTC"
			if newTz.Valid && newTz.String != "" {
				tz = newTz.String
			}
			next, err := computeNextRun(newCron.String, tz)
			if err != nil {
				writeError(w, http.StatusBadRequest, err.Error())
				return
			}
			params.CronExpression = newCron
			params.Timezone = pgtype.Text{String: tz, Valid: true}
			params.NextRunAt = pgtype.Timestamptz{Time: next, Valid: true}
		}
		// else: cron cleared — CronExpression/Timezone/NextRunAt stay at their
		// zero pgtype value, which UpdateWorkQueue writes as NULL under SetCron.
	}

	queue, err := h.Queries.UpdateWorkQueue(r.Context(), params)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to update work queue")
		return
	}

	h.publishWorkQueueUpdated(r, workspaceID, queue.ID)
	writeJSON(w, http.StatusOK, map[string]any{"queue": queue})
}

func (h *Handler) DeleteWorkQueue(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}

	if err := h.Queries.DeleteWorkQueue(r.Context(), db.DeleteWorkQueueParams{
		ID:          queue.ID,
		WorkspaceID: queue.WorkspaceID,
	}); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to delete work queue")
		return
	}

	h.publishWorkQueueUpdated(r, workspaceID, queue.ID)
	w.WriteHeader(http.StatusNoContent)
}

// ── Items ────────────────────────────────────────────────────────────────────

type WorkQueueItemInput struct {
	Kind    string  `json:"kind"`
	Title   *string `json:"title"`
	Body    *string `json:"body"`
	IssueID *string `json:"issue_id"`
	AgentID *string `json:"agent_id"`
}

type CreateWorkQueueItemsRequest struct {
	Items []WorkQueueItemInput `json:"items"`
}

func (h *Handler) CreateWorkQueueItems(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}

	var req CreateWorkQueueItemsRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if len(req.Items) == 0 {
		writeError(w, http.StatusBadRequest, "items is required")
		return
	}

	type preparedItem struct {
		kind    string
		title   pgtype.Text
		body    pgtype.Text
		issueID pgtype.UUID
		agentID pgtype.UUID
	}
	prepared := make([]preparedItem, len(req.Items))
	for i, item := range req.Items {
		if item.Kind != "prompt" && item.Kind != "issue" {
			writeError(w, http.StatusBadRequest, fmt.Sprintf("items[%d].kind must be prompt or issue", i))
			return
		}
		p := preparedItem{kind: item.Kind, title: ptrToText(item.Title), body: ptrToText(item.Body)}
		if item.Kind == "prompt" && (item.Title == nil || *item.Title == "") {
			writeError(w, http.StatusBadRequest, fmt.Sprintf("items[%d].title is required for prompt items", i))
			return
		}
		if item.Kind == "issue" {
			if item.IssueID == nil || *item.IssueID == "" {
				writeError(w, http.StatusBadRequest, fmt.Sprintf("items[%d].issue_id is required for issue items", i))
				return
			}
			parsed, iok := parseUUIDOrBadRequest(w, *item.IssueID, fmt.Sprintf("items[%d].issue_id", i))
			if !iok {
				return
			}
			if !h.issueExistsInWorkspace(r, parsed, queue.WorkspaceID) {
				writeError(w, http.StatusBadRequest, fmt.Sprintf("items[%d].issue_id must be a valid issue in this workspace", i))
				return
			}
			p.issueID = parsed
		}
		if item.AgentID != nil && *item.AgentID != "" {
			parsed, aok := parseUUIDOrBadRequest(w, *item.AgentID, fmt.Sprintf("items[%d].agent_id", i))
			if !aok {
				return
			}
			if !h.agentExistsInWorkspace(r, parsed, queue.WorkspaceID) {
				writeError(w, http.StatusBadRequest, fmt.Sprintf("items[%d].agent_id must be a valid agent in this workspace", i))
				return
			}
			p.agentID = parsed
		}
		prepared[i] = p
	}

	maxSeq, err := h.Queries.MaxWorkQueueItemSeq(r.Context(), queue.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to load work queue items")
		return
	}

	tx, err := h.TxStarter.Begin(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to add items")
		return
	}
	defer tx.Rollback(r.Context())
	qtx := h.Queries.WithTx(tx)

	created := make([]db.WorkQueueItem, len(prepared))
	for i, p := range prepared {
		it, err := qtx.CreateWorkQueueItem(r.Context(), db.CreateWorkQueueItemParams{
			QueueID:     queue.ID,
			WorkspaceID: queue.WorkspaceID,
			Seq:         maxSeq + int32(i) + 1,
			Kind:        p.kind,
			Title:       p.title,
			Body:        p.body,
			IssueID:     p.issueID,
			AgentID:     p.agentID,
		})
		if err != nil {
			writeError(w, http.StatusInternalServerError, "failed to add work queue item")
			return
		}
		created[i] = it
	}

	if err := tx.Commit(r.Context()); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to add items")
		return
	}

	h.publishWorkQueueUpdated(r, workspaceID, queue.ID)
	writeJSON(w, http.StatusCreated, map[string]any{"items": created})
}

type UpdateWorkQueueItemRequest struct {
	Title   *string `json:"title"`
	Body    *string `json:"body"`
	AgentID *string `json:"agent_id"`
}

func (h *Handler) UpdateWorkQueueItem(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	itemID := chi.URLParam(r, "itemId")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}
	item, ok := h.loadWorkQueueItemInWorkspace(w, r, itemID, workspaceID)
	if !ok {
		return
	}
	if item.QueueID != queue.ID {
		writeError(w, http.StatusNotFound, "work queue item not found")
		return
	}

	bodyBytes, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, http.StatusBadRequest, "failed to read request body")
		return
	}
	var req UpdateWorkQueueItemRequest
	if err := json.Unmarshal(bodyBytes, &req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	var rawFields map[string]json.RawMessage
	json.Unmarshal(bodyBytes, &rawFields)

	params := db.UpdateWorkQueueItemParams{
		ID:          item.ID,
		WorkspaceID: item.WorkspaceID,
		Title:       ptrToText(req.Title),
		Body:        ptrToText(req.Body),
	}
	if _, sent := rawFields["agent_id"]; sent {
		params.SetAgent = true
		if req.AgentID != nil && *req.AgentID != "" {
			parsed, aok := parseUUIDOrBadRequest(w, *req.AgentID, "agent_id")
			if !aok {
				return
			}
			if !h.agentExistsInWorkspace(r, parsed, item.WorkspaceID) {
				writeError(w, http.StatusBadRequest, "agent_id must be a valid agent in this workspace")
				return
			}
			params.AgentID = parsed
		}
	}

	updated, err := h.Queries.UpdateWorkQueueItem(r.Context(), params)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			// UPDATE ... WHERE status = 'pending' matched no row: the item
			// exists (we just loaded it) but is no longer pending.
			writeError(w, http.StatusBadRequest, "item is not pending")
			return
		}
		writeError(w, http.StatusInternalServerError, "failed to update work queue item")
		return
	}

	h.publishWorkQueueUpdated(r, workspaceID, queue.ID)
	writeJSON(w, http.StatusOK, map[string]any{"item": updated})
}

func (h *Handler) DeleteWorkQueueItem(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	itemID := chi.URLParam(r, "itemId")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}
	item, ok := h.loadWorkQueueItemInWorkspace(w, r, itemID, workspaceID)
	if !ok {
		return
	}
	if item.QueueID != queue.ID {
		writeError(w, http.StatusNotFound, "work queue item not found")
		return
	}

	rows, err := h.Queries.DeleteWorkQueueItem(r.Context(), db.DeleteWorkQueueItemParams{
		ID:          item.ID,
		WorkspaceID: item.WorkspaceID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to delete work queue item")
		return
	}
	if rows == 0 {
		// The item existed at load time (status <> running filter excluded
		// it) — it must be running now.
		writeError(w, http.StatusConflict, "cannot delete a running item")
		return
	}

	h.publishWorkQueueUpdated(r, workspaceID, queue.ID)
	w.WriteHeader(http.StatusNoContent)
}

// RetryWorkQueueItem re-enqueues a failed item as pending, clearing its error
// and task linkage. It does not restart an idle queue — the user presses
// Start again (or a running drain picks the item up on its next step).
func (h *Handler) RetryWorkQueueItem(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	itemID := chi.URLParam(r, "itemId")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}
	item, ok := h.loadWorkQueueItemInWorkspace(w, r, itemID, workspaceID)
	if !ok {
		return
	}
	if item.QueueID != queue.ID {
		writeError(w, http.StatusNotFound, "work queue item not found")
		return
	}

	rows, err := h.Queries.RetryWorkQueueItem(r.Context(), db.RetryWorkQueueItemParams{
		ID:          item.ID,
		WorkspaceID: item.WorkspaceID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to retry work queue item")
		return
	}
	if rows == 0 {
		writeError(w, http.StatusBadRequest, "item is not failed")
		return
	}

	h.publishWorkQueueUpdated(r, workspaceID, queue.ID)
	w.WriteHeader(http.StatusNoContent)
}

type ReorderWorkQueueItemsRequest struct {
	Order []string `json:"order"`
}

func (h *Handler) ReorderWorkQueueItems(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}

	var req ReorderWorkQueueItemsRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	if len(req.Order) == 0 {
		writeError(w, http.StatusBadRequest, "order is required")
		return
	}
	itemUUIDs, ok := parseUUIDSliceOrBadRequest(w, req.Order, "order")
	if !ok {
		return
	}

	tx, err := h.TxStarter.Begin(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to reorder items")
		return
	}
	defer tx.Rollback(r.Context())
	qtx := h.Queries.WithTx(tx)

	for i, itemUUID := range itemUUIDs {
		if err := qtx.UpdateWorkQueueItemSeq(r.Context(), db.UpdateWorkQueueItemSeqParams{
			ID:      itemUUID,
			QueueID: queue.ID,
			Seq:     int32(i) + 1,
		}); err != nil {
			writeError(w, http.StatusInternalServerError, "failed to reorder items")
			return
		}
	}

	if err := tx.Commit(r.Context()); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to reorder items")
		return
	}

	h.publishWorkQueueUpdated(r, workspaceID, queue.ID)
	w.WriteHeader(http.StatusNoContent)
}

// ── Verbs ────────────────────────────────────────────────────────────────────

type StartWorkQueueRequest struct {
	StartAt *string `json:"start_at"`
}

func (h *Handler) StartWorkQueue(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}

	var req StartWorkQueueRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil && !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	var startAt *time.Time
	if req.StartAt != nil && *req.StartAt != "" {
		t, err := time.Parse(time.RFC3339, *req.StartAt)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid start_at")
			return
		}
		startAt = &t
	}

	// A queue can only start if every pending item resolves to an agent —
	// either its own override or the queue's default_agent_id.
	items, err := h.Queries.ListWorkQueueItems(r.Context(), db.ListWorkQueueItemsParams{
		QueueID:     queue.ID,
		WorkspaceID: queue.WorkspaceID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to load work queue items")
		return
	}
	for _, item := range items {
		if item.Status == "pending" && !item.AgentID.Valid && !queue.DefaultAgentID.Valid {
			writeError(w, http.StatusBadRequest, "queue has pending items with no agent resolved: set an item agent or a queue default agent")
			return
		}
	}

	updated, err := h.WorkQueueService.Start(r.Context(), queue, startAt)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to start work queue")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"queue": updated})
}

func (h *Handler) PauseWorkQueue(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}
	if queue.Status != "running" && queue.Status != "scheduled" {
		writeError(w, http.StatusBadRequest, "queue is not running or scheduled")
		return
	}

	updated, err := h.WorkQueueService.Pause(r.Context(), queue)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to pause work queue")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"queue": updated})
}

func (h *Handler) ResumeWorkQueue(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}
	if queue.Status != "paused" {
		writeError(w, http.StatusBadRequest, "queue is not paused")
		return
	}

	updated, err := h.WorkQueueService.Resume(r.Context(), queue)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to resume work queue")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"queue": updated})
}

func (h *Handler) ClearFinishedWorkQueueItems(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	workspaceID := h.resolveWorkspaceID(r)

	queue, ok := h.loadWorkQueueInWorkspace(w, r, id, workspaceID)
	if !ok {
		return
	}

	deleted, err := h.Queries.DeleteFinishedWorkQueueItems(r.Context(), db.DeleteFinishedWorkQueueItemsParams{
		QueueID:     queue.ID,
		WorkspaceID: queue.WorkspaceID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to clear finished items")
		return
	}
	if deleted > 0 {
		h.publishWorkQueueUpdated(r, workspaceID, queue.ID)
	}
	writeJSON(w, http.StatusOK, map[string]any{"deleted": deleted})
}
