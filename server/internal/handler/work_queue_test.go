package handler

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// createTestWorkQueue creates a work queue via the handler under test and
// registers cleanup. Returns the decoded queue object (json-tagged fields
// from db.WorkQueue).
func createTestWorkQueue(t *testing.T, body map[string]any) map[string]any {
	t.Helper()
	w := httptest.NewRecorder()
	testHandler.CreateWorkQueue(w, newRequest("POST", "/api/queues", body))
	if w.Code != http.StatusCreated {
		t.Fatalf("CreateWorkQueue: expected 201, got %d: %s", w.Code, w.Body.String())
	}
	var resp struct {
		Queue map[string]any `json:"queue"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode CreateWorkQueue response: %v", err)
	}
	id, _ := resp.Queue["id"].(string)
	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM work_queue WHERE id = $1`, id)
	})
	return resp.Queue
}

func getTestWorkQueue(t *testing.T, id string) (map[string]any, []map[string]any, int) {
	t.Helper()
	w := httptest.NewRecorder()
	testHandler.GetWorkQueue(w, withURLParam(newRequest("GET", "/api/queues/"+id, nil), "id", id))
	if w.Code != http.StatusOK {
		return nil, nil, w.Code
	}
	var resp struct {
		Queue map[string]any   `json:"queue"`
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode GetWorkQueue response: %v", err)
	}
	return resp.Queue, resp.Items, w.Code
}

// TestWorkQueueLifecycle drives the full happy path across the REST surface:
// create -> list -> add 2 items -> reorder -> get (order) -> start (running
// via GET) -> pause -> clear-finished (no-op) -> delete.
func TestWorkQueueLifecycle(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}

	agentID := createHandlerTestAgent(t, "work-queue-lifecycle-agent", []byte(`[]`))

	queue := createTestWorkQueue(t, map[string]any{
		"name":             "Lifecycle queue",
		"default_agent_id": agentID,
	})
	queueID, _ := queue["id"].(string)
	if queueID == "" {
		t.Fatalf("CreateWorkQueue: missing id in response %v", queue)
	}
	if status, _ := queue["status"].(string); status != "idle" {
		t.Fatalf("CreateWorkQueue: expected status idle, got %v", queue["status"])
	}

	// List
	w := httptest.NewRecorder()
	testHandler.ListWorkQueues(w, newRequest("GET", "/api/queues", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("ListWorkQueues: expected 200, got %d: %s", w.Code, w.Body.String())
	}
	var listResp struct {
		Queues []map[string]any `json:"queues"`
		Total  int              `json:"total"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &listResp); err != nil {
		t.Fatalf("decode ListWorkQueues response: %v", err)
	}
	found := false
	for _, q := range listResp.Queues {
		if q["id"] == queueID {
			found = true
			counts, ok := q["item_counts"].(map[string]any)
			if !ok {
				t.Fatalf("ListWorkQueues: expected item_counts object, got %v", q["item_counts"])
			}
			if counts["pending"] != float64(0) {
				t.Errorf("ListWorkQueues: expected 0 pending items for a fresh queue, got %v", counts["pending"])
			}
		}
	}
	if !found {
		t.Fatalf("ListWorkQueues: queue %s missing from list", queueID)
	}

	// Add 2 items
	w = httptest.NewRecorder()
	addReq := withURLParam(newRequest("POST", "/api/queues/"+queueID+"/items", map[string]any{
		"items": []map[string]any{
			{"kind": "prompt", "title": "Item A"},
			{"kind": "prompt", "title": "Item B"},
		},
	}), "id", queueID)
	testHandler.CreateWorkQueueItems(w, addReq)
	if w.Code != http.StatusCreated {
		t.Fatalf("CreateWorkQueueItems: expected 201, got %d: %s", w.Code, w.Body.String())
	}
	var itemsResp struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &itemsResp); err != nil {
		t.Fatalf("decode CreateWorkQueueItems response: %v", err)
	}
	if len(itemsResp.Items) != 2 {
		t.Fatalf("CreateWorkQueueItems: expected 2 items, got %d", len(itemsResp.Items))
	}
	itemA, _ := itemsResp.Items[0]["id"].(string)
	itemB, _ := itemsResp.Items[1]["id"].(string)

	// Reorder: swap B before A
	w = httptest.NewRecorder()
	reorderReq := withURLParam(newRequest("POST", "/api/queues/"+queueID+"/items/reorder", map[string]any{
		"order": []string{itemB, itemA},
	}), "id", queueID)
	testHandler.ReorderWorkQueueItems(w, reorderReq)
	if w.Code != http.StatusNoContent {
		t.Fatalf("ReorderWorkQueueItems: expected 204, got %d: %s", w.Code, w.Body.String())
	}

	// Get: assert item order reflects the reorder
	_, items, code := getTestWorkQueue(t, queueID)
	if code != http.StatusOK {
		t.Fatalf("GetWorkQueue: expected 200, got %d", code)
	}
	if len(items) != 2 || items[0]["id"] != itemB || items[1]["id"] != itemA {
		t.Fatalf("GetWorkQueue: expected order [B, A], got %v", items)
	}

	// Start: assert status running via a subsequent GET
	w = httptest.NewRecorder()
	startReq := withURLParam(newRequest("POST", "/api/queues/"+queueID+"/start", nil), "id", queueID)
	testHandler.StartWorkQueue(w, startReq)
	if w.Code != http.StatusOK {
		t.Fatalf("StartWorkQueue: expected 200, got %d: %s", w.Code, w.Body.String())
	}
	gotQueue, _, code := getTestWorkQueue(t, queueID)
	if code != http.StatusOK {
		t.Fatalf("GetWorkQueue after start: expected 200, got %d", code)
	}
	if status, _ := gotQueue["status"].(string); status != "running" {
		t.Fatalf("GetWorkQueue after start: expected status running, got %v", gotQueue["status"])
	}

	// Pause
	w = httptest.NewRecorder()
	pauseReq := withURLParam(newRequest("POST", "/api/queues/"+queueID+"/pause", nil), "id", queueID)
	testHandler.PauseWorkQueue(w, pauseReq)
	if w.Code != http.StatusOK {
		t.Fatalf("PauseWorkQueue: expected 200, got %d: %s", w.Code, w.Body.String())
	}
	var pauseResp struct {
		Queue map[string]any `json:"queue"`
	}
	json.Unmarshal(w.Body.Bytes(), &pauseResp)
	if status, _ := pauseResp.Queue["status"].(string); status != "paused" {
		t.Fatalf("PauseWorkQueue: expected status paused, got %v", pauseResp.Queue["status"])
	}

	// Clear-finished on an empty finished set
	w = httptest.NewRecorder()
	clearReq := withURLParam(newRequest("POST", "/api/queues/"+queueID+"/clear-finished", nil), "id", queueID)
	testHandler.ClearFinishedWorkQueueItems(w, clearReq)
	if w.Code != http.StatusOK {
		t.Fatalf("ClearFinishedWorkQueueItems: expected 200, got %d: %s", w.Code, w.Body.String())
	}
	var clearResp struct {
		Deleted int64 `json:"deleted"`
	}
	json.Unmarshal(w.Body.Bytes(), &clearResp)
	if clearResp.Deleted != 0 {
		t.Fatalf("ClearFinishedWorkQueueItems: expected deleted=0, got %d", clearResp.Deleted)
	}

	// Delete queue
	w = httptest.NewRecorder()
	deleteReq := withURLParam(newRequest("DELETE", "/api/queues/"+queueID, nil), "id", queueID)
	testHandler.DeleteWorkQueue(w, deleteReq)
	if w.Code != http.StatusNoContent {
		t.Fatalf("DeleteWorkQueue: expected 204, got %d: %s", w.Code, w.Body.String())
	}
	if _, _, code := getTestWorkQueue(t, queueID); code != http.StatusNotFound {
		t.Fatalf("GetWorkQueue after delete: expected 404, got %d", code)
	}
}

// TestCreateWorkQueueItems_ValidatesKindRequirements locks in the item-kind
// validation rules: prompt items require a non-empty title, issue items
// require an issue_id.
func TestCreateWorkQueueItems_ValidatesKindRequirements(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}

	queue := createTestWorkQueue(t, map[string]any{"name": "Validation queue"})
	queueID, _ := queue["id"].(string)

	cases := []struct {
		name string
		item map[string]any
	}{
		{"prompt without title", map[string]any{"kind": "prompt"}},
		{"issue without issue_id", map[string]any{"kind": "issue"}},
		{"invalid kind", map[string]any{"kind": "bogus", "title": "x"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			req := withURLParam(newRequest("POST", "/api/queues/"+queueID+"/items", map[string]any{
				"items": []map[string]any{c.item},
			}), "id", queueID)
			testHandler.CreateWorkQueueItems(w, req)
			if w.Code != http.StatusBadRequest {
				t.Fatalf("CreateWorkQueueItems(%s): expected 400, got %d: %s", c.name, w.Code, w.Body.String())
			}
		})
	}
}

// TestWorkQueueItem_NotPendingRejectsUpdateAndDelete locks in that a running
// item cannot be edited (400, since the underlying UPDATE ... WHERE
// status='pending' matches no row) or deleted (409).
func TestWorkQueueItem_NotPendingRejectsUpdateAndDelete(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}

	agentID := createHandlerTestAgent(t, "work-queue-running-item-agent", []byte(`[]`))
	queue := createTestWorkQueue(t, map[string]any{
		"name":             "Running item queue",
		"default_agent_id": agentID,
	})
	queueID, _ := queue["id"].(string)

	w := httptest.NewRecorder()
	addReq := withURLParam(newRequest("POST", "/api/queues/"+queueID+"/items", map[string]any{
		"items": []map[string]any{{"kind": "prompt", "title": "Only item"}},
	}), "id", queueID)
	testHandler.CreateWorkQueueItems(w, addReq)
	if w.Code != http.StatusCreated {
		t.Fatalf("CreateWorkQueueItems: expected 201, got %d: %s", w.Code, w.Body.String())
	}
	var itemsResp struct {
		Items []map[string]any `json:"items"`
	}
	json.Unmarshal(w.Body.Bytes(), &itemsResp)
	itemID, _ := itemsResp.Items[0]["id"].(string)

	// Start so the (only) item transitions to running.
	w = httptest.NewRecorder()
	startReq := withURLParam(newRequest("POST", "/api/queues/"+queueID+"/start", nil), "id", queueID)
	testHandler.StartWorkQueue(w, startReq)
	if w.Code != http.StatusOK {
		t.Fatalf("StartWorkQueue: expected 200, got %d: %s", w.Code, w.Body.String())
	}
	_, items, code := getTestWorkQueue(t, queueID)
	if code != http.StatusOK || len(items) != 1 || items[0]["status"] != "running" {
		t.Fatalf("expected the single item to be running after start, got %v (code %d)", items, code)
	}

	// PATCH a running item -> 400
	w = httptest.NewRecorder()
	patchReq := withURLParams(newRequest("PATCH", "/api/queues/"+queueID+"/items/"+itemID, map[string]any{"title": "renamed"}),
		"id", queueID, "itemId", itemID)
	testHandler.UpdateWorkQueueItem(w, patchReq)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("UpdateWorkQueueItem on running item: expected 400, got %d: %s", w.Code, w.Body.String())
	}

	// DELETE a running item -> 409
	w = httptest.NewRecorder()
	deleteReq := withURLParams(newRequest("DELETE", "/api/queues/"+queueID+"/items/"+itemID, nil),
		"id", queueID, "itemId", itemID)
	testHandler.DeleteWorkQueueItem(w, deleteReq)
	if w.Code != http.StatusConflict {
		t.Fatalf("DeleteWorkQueueItem on running item: expected 409, got %d: %s", w.Code, w.Body.String())
	}
}

// TestWorkQueue_CrossWorkspace404 locks in that a queue id from another
// workspace 404s rather than leaking data across workspace boundaries.
func TestWorkQueue_CrossWorkspace404(t *testing.T) {
	if testHandler == nil || testPool == nil {
		t.Skip("database not available")
	}

	otherWS := createOtherTestWorkspace(t)
	var otherQueueID string
	if err := testPool.QueryRow(context.Background(), `
		INSERT INTO work_queue (workspace_id, name, created_by)
		VALUES ($1, $2, $3)
		RETURNING id
	`, otherWS, "Other workspace queue", testUserID).Scan(&otherQueueID); err != nil {
		t.Fatalf("seed other-workspace queue: %v", err)
	}
	t.Cleanup(func() {
		testPool.Exec(context.Background(), `DELETE FROM work_queue WHERE id = $1`, otherQueueID)
	})

	// newRequest defaults X-Workspace-ID to testWorkspaceID, so this request
	// asks for otherQueueID under the wrong workspace.
	w := httptest.NewRecorder()
	req := withURLParam(newRequest("GET", "/api/queues/"+otherQueueID, nil), "id", otherQueueID)
	testHandler.GetWorkQueue(w, req)
	if w.Code != http.StatusNotFound {
		t.Fatalf("GetWorkQueue cross-workspace: expected 404, got %d: %s", w.Code, w.Body.String())
	}
}
