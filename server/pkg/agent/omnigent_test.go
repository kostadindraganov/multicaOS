package agent

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestOmnigentAgentsToModels(t *testing.T) {
	agents := []omnigentAgentObject{
		{ID: "ag_2", Name: "polly", Builtin: true, Harness: "claude-sdk"},
		{ID: "ag_1", Name: "claude-native-ui", Builtin: true, Harness: "claude-native"},
		{ID: "ag_3", Name: "codex-native-ui", Builtin: true, Harness: "codex-native"},
		{ID: "ag_4", Name: "debby", Builtin: true},
		{ID: "ag_5", Name: "my-reviewer", Builtin: false},
		{ID: "ag_6", Name: ""}, // dropped
	}
	models := omnigentAgentsToModels(agents)

	gotIDs := make([]string, 0, len(models))
	for _, m := range models {
		gotIDs = append(gotIDs, m.ID)
	}
	wantIDs := []string{"claude-native-ui", "codex-native-ui", "debby", "my-reviewer", "polly"}
	if len(gotIDs) != len(wantIDs) {
		t.Fatalf("got %d models %v, want %d", len(gotIDs), gotIDs, len(wantIDs))
	}
	for i := range wantIDs {
		if gotIDs[i] != wantIDs[i] {
			t.Errorf("models[%d].ID = %q, want %q (all: %v)", i, gotIDs[i], wantIDs[i], gotIDs)
		}
	}

	if models[0].Label != "Claude (native CLI)" {
		t.Errorf("claude label = %q, want %q", models[0].Label, "Claude (native CLI)")
	}
	if !models[0].Default {
		t.Errorf("claude-native-ui should carry the Default badge")
	}
	if models[0].Provider != "native" || models[1].Provider != "native" {
		t.Errorf("native wrappers should be grouped under provider %q", "native")
	}
	for _, m := range models[2:] {
		if m.Provider != "custom" {
			t.Errorf("%s grouped as %q, want custom", m.ID, m.Provider)
		}
		if m.Default {
			t.Errorf("%s should not be the default", m.ID)
		}
	}
	if models[2].Label != "Debby" || models[4].Label != "Polly" {
		t.Errorf("plain names should be title-cased: got %q / %q", models[2].Label, models[4].Label)
	}
	if models[3].Label != "my-reviewer" {
		t.Errorf("hyphenated custom name should pass through unchanged, got %q", models[3].Label)
	}
}

func TestOmnigentResolveAgentID(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/agents" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"data": []map[string]any{
				{"id": "ag_claude", "name": "claude-native-ui", "builtin": true},
				{"id": "ag_polly", "name": "polly", "builtin": true},
			},
		})
	}))
	defer srv.Close()

	ctx := context.Background()
	for _, tc := range []struct {
		model, want string
	}{
		{"polly", "ag_polly"},
		{"ag_polly", "ag_polly"},
		{"", "ag_claude"}, // empty model falls back to the seeded claude agent
	} {
		got, err := omnigentResolveAgentID(ctx, srv.URL, tc.model)
		if err != nil {
			t.Fatalf("resolve(%q): %v", tc.model, err)
		}
		if got != tc.want {
			t.Errorf("resolve(%q) = %q, want %q", tc.model, got, tc.want)
		}
	}

	if _, err := omnigentResolveAgentID(ctx, srv.URL, "nope"); err == nil {
		t.Fatalf("resolve(nope) should fail")
	} else if !strings.Contains(err.Error(), "polly") {
		t.Errorf("not-found error should list available agents, got: %v", err)
	}
}

// drainSession collects everything a consumeStream run produces.
func drainSession(t *testing.T, msgCh chan Message, resCh chan Result) ([]Message, Result) {
	t.Helper()
	var msgs []Message
	for m := range msgCh {
		msgs = append(msgs, m)
	}
	select {
	case res := <-resCh:
		return msgs, res
	case <-time.After(5 * time.Second):
		t.Fatalf("no Result within 5s")
		return nil, Result{}
	}
}

func runConsumeStream(t *testing.T, baseURL, sse string) ([]Message, Result) {
	t.Helper()
	b := &omnigentBackend{cfg: Config{Logger: slog.Default()}}
	runCtx, cancel := context.WithCancel(context.Background())
	msgCh := make(chan Message, 256)
	resCh := make(chan Result, 1)
	go b.consumeStream(runCtx, cancel, baseURL, "conv_test", io.NopCloser(strings.NewReader(sse)), 0, msgCh, resCh)
	return drainSession(t, msgCh, resCh)
}

func TestOmnigentConsumeStreamCompletedTurn(t *testing.T) {
	// Event shapes mirror the live server: session.status carries `status`
	// and session.usage carries `usage_by_model` at the TOP level of the
	// event, not nested under `data` (the nested form is legacy fallback,
	// covered by TestOmnigentConsumeStreamLegacyDataNesting). Reading only
	// data.status left every turn stuck in "running" forever.
	sse := strings.Join([]string{
		`data: {"type":"session.status","conversation_id":"conv_test","status":"running"}`,
		``,
		`data: {"type":"response.output_text.delta","delta":"Hel"}`,
		``,
		`data: {"type":"response.output_text.delta","delta":"lo"}`,
		``,
		`data: {"type":"response.output_item.done","item":{"type":"function_call","status":"completed","name":"read_file","call_id":"call_1","arguments":"{\"path\":\"main.go\"}"}}`,
		``,
		`data: {"type":"response.output_item.done","item":{"type":"function_call_output","call_id":"call_1","output":"package main"}}`,
		``,
		`data: {"type":"response.output_item.done","item":{"type":"message","status":"completed","role":"assistant","content":[{"type":"output_text","text":"Hello from polly"}]}}`,
		``,
		`data: {"type":"session.usage","conversation_id":"conv_test","usage_by_model":{"claude-sonnet-5":{"input_tokens":120,"output_tokens":45}}}`,
		``,
		`data: {"type":"response.completed","response":{}}`,
		``,
		`data: {"type":"session.status","conversation_id":"conv_test","status":"idle"}`,
		``,
		`data: [DONE]`,
		``,
	}, "\n")

	msgs, res := runConsumeStream(t, "http://127.0.0.1:1", sse)

	if res.Status != "completed" {
		t.Fatalf("status = %q (err %q), want completed", res.Status, res.Error)
	}
	if res.Output != "Hello from polly" {
		t.Errorf("output = %q, want the final message text (deltas must not double-count)", res.Output)
	}
	if res.SessionID != "conv_test" {
		t.Errorf("session id = %q, want conv_test", res.SessionID)
	}
	if u, ok := res.Usage["claude-sonnet-5"]; !ok || u.InputTokens != 120 || u.OutputTokens != 45 {
		t.Errorf("usage = %+v, want claude-sonnet-5 {120,45}", res.Usage)
	}

	var sawToolUse, sawToolResult, sawText bool
	for _, m := range msgs {
		switch m.Type {
		case MessageToolUse:
			sawToolUse = m.Tool == "read_file" && m.CallID == "call_1" && m.Input["path"] == "main.go"
		case MessageToolResult:
			sawToolResult = m.CallID == "call_1" && m.Output == "package main"
		case MessageText:
			sawText = m.Content == "Hello from polly"
		}
	}
	if !sawToolUse || !sawToolResult || !sawText {
		t.Errorf("missing streamed messages: tool_use=%v tool_result=%v text=%v (%+v)", sawToolUse, sawToolResult, sawText, msgs)
	}
	if msgs[0].Type != MessageStatus || msgs[0].SessionID != "conv_test" {
		t.Errorf("first message should pin the session id for early resume, got %+v", msgs[0])
	}
}

// TestOmnigentConsumeStreamLegacyDataNesting keeps the nested data.status /
// data.model usage fallback alive for older server payloads.
func TestOmnigentConsumeStreamLegacyDataNesting(t *testing.T) {
	sse := strings.Join([]string{
		`data: {"type":"session.status","data":{"status":"running"}}`,
		``,
		`data: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"legacy"}]}}`,
		``,
		`data: {"type":"session.usage","data":{"model":"claude-sonnet-5","cumulative_input_tokens":120,"cumulative_output_tokens":45}}`,
		``,
		`data: {"type":"session.status","data":{"status":"idle"}}`,
		``,
	}, "\n")

	_, res := runConsumeStream(t, "http://127.0.0.1:1", sse)
	if res.Status != "completed" {
		t.Fatalf("status = %q (err %q), want completed", res.Status, res.Error)
	}
	if res.Output != "legacy" {
		t.Errorf("output = %q, want the message text", res.Output)
	}
	if u, ok := res.Usage["claude-sonnet-5"]; !ok || u.InputTokens != 120 || u.OutputTokens != 45 {
		t.Errorf("usage = %+v, want claude-sonnet-5 {120,45}", res.Usage)
	}
}

// TestOmnigentConsumeStreamDedupesDoubleDoneToolCalls reproduces the live
// server emitting response.output_item.done TWICE per tool call: once with
// item.status=in_progress when the call is issued and once with
// status=completed when its output lands (distinct item ids, same call_id).
// Without call_id dedupe every tool call showed up twice in the transcript.
func TestOmnigentConsumeStreamDedupesDoubleDoneToolCalls(t *testing.T) {
	sse := strings.Join([]string{
		`data: {"type":"session.status","status":"running"}`,
		``,
		`data: {"type":"response.output_item.done","item":{"id":"fc_1","type":"function_call","status":"in_progress","name":"sys_os_shell","call_id":"toolu_1","arguments":"{\"command\":\"echo hi\"}"}}`,
		``,
		`data: {"type":"response.output_item.done","item":{"id":"fc_2","type":"function_call","status":"completed","name":"sys_os_shell","call_id":"toolu_1","arguments":"{\"command\":\"echo hi\"}"}}`,
		``,
		`data: {"type":"response.output_item.done","item":{"type":"function_call_output","call_id":"toolu_1","output":"hi"}}`,
		``,
		`data: {"type":"response.output_item.done","item":{"type":"function_call_output","call_id":"toolu_1","output":"hi"}}`,
		``,
		`data: {"type":"response.output_item.done","item":{"type":"message","status":"completed","role":"assistant","content":[{"type":"output_text","text":"done"}]}}`,
		``,
		`data: {"type":"session.status","status":"idle"}`,
		``,
	}, "\n")

	msgs, res := runConsumeStream(t, "http://127.0.0.1:1", sse)
	if res.Status != "completed" {
		t.Fatalf("status = %q (err %q), want completed", res.Status, res.Error)
	}
	var toolUses, toolResults int
	for _, m := range msgs {
		switch m.Type {
		case MessageToolUse:
			toolUses++
		case MessageToolResult:
			toolResults++
		}
	}
	if toolUses != 1 {
		t.Errorf("tool_use emitted %d times, want 1 (double-done dedupe by call_id)", toolUses)
	}
	if toolResults != 1 {
		t.Errorf("tool_result emitted %d times, want 1", toolResults)
	}
}

func TestOmnigentConsumeStreamFailedTurn(t *testing.T) {
	sse := strings.Join([]string{
		`data: {"type":"session.status","data":{"status":"running"}}`,
		``,
		`data: {"type":"response.failed","error":{"message":"model quota exhausted"}}`,
		``,
	}, "\n")

	_, res := runConsumeStream(t, "http://127.0.0.1:1", sse)
	if res.Status != "failed" {
		t.Fatalf("status = %q, want failed", res.Status)
	}
	if !strings.Contains(res.Error, "model quota exhausted") {
		t.Errorf("error = %q, want the upstream message", res.Error)
	}
}

func TestOmnigentConsumeStreamNativeDeltasOnly(t *testing.T) {
	// Native terminal harnesses can stream deltas and settle to idle without
	// a final message item; the accumulated delta text must survive.
	sse := strings.Join([]string{
		`data: {"type":"session.status","data":{"status":"running"}}`,
		``,
		`data: {"type":"response.output_text.delta","delta":"streamed answer"}`,
		``,
		`data: {"type":"session.status","data":{"status":"idle"}}`,
		``,
	}, "\n")

	_, res := runConsumeStream(t, "http://127.0.0.1:1", sse)
	if res.Status != "completed" {
		t.Fatalf("status = %q, want completed", res.Status)
	}
	if res.Output != "streamed answer" {
		t.Errorf("output = %q, want the accumulated deltas", res.Output)
	}
}

func TestOmnigentConsumeStreamStreamClosedEarly(t *testing.T) {
	sse := `data: {"type":"session.status","data":{"status":"running"}}` + "\n"
	_, res := runConsumeStream(t, "http://127.0.0.1:1", sse)
	if res.Status != "failed" {
		t.Fatalf("status = %q, want failed when the stream dies mid-turn", res.Status)
	}
}

func TestOmnigentConsumeStreamAutoAcceptsElicitation(t *testing.T) {
	var accepted atomic.Bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/sessions/conv_child/elicitations/elicit_1/resolve" {
			var body struct {
				Action string `json:"action"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body.Action == "accept" {
				accepted.Store(true)
			}
			w.WriteHeader(http.StatusAccepted)
			_, _ = w.Write([]byte(`{"queued": false}`))
			return
		}
		t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	sse := strings.Join([]string{
		`data: {"type":"session.status","data":{"status":"running"}}`,
		``,
		`data: {"type":"response.elicitation_request","elicitation_id":"elicit_1","params":{"target_session_id":"conv_child"}}`,
		``,
		`data: {"type":"session.status","data":{"status":"idle"}}`,
		``,
	}, "\n")

	_, res := runConsumeStream(t, srv.URL, sse)
	if res.Status != "completed" {
		t.Fatalf("status = %q, want completed", res.Status)
	}
	if !accepted.Load() {
		t.Errorf("elicitation was not auto-accepted against the child session")
	}
}

func TestOmnigentStreamEventErrorMessage(t *testing.T) {
	for _, tc := range []struct {
		payload, want string
	}{
		{`{"type":"response.error","message":"boom"}`, "boom"},
		{`{"type":"response.failed","error":"plain string"}`, "plain string"},
		{`{"type":"response.failed","error":{"message":"nested"}}`, "nested"},
	} {
		var ev omnigentStreamEvent
		if err := json.Unmarshal([]byte(tc.payload), &ev); err != nil {
			t.Fatalf("unmarshal %s: %v", tc.payload, err)
		}
		if got := ev.ErrorMessage(); got != tc.want {
			t.Errorf("ErrorMessage(%s) = %q, want %q", tc.payload, got, tc.want)
		}
	}
}
