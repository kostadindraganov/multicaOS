package agent

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os/exec"
	"sort"
	"strings"
	"sync"
	"time"
)

// omnigentBackend implements Backend by driving the local Omnigent server's
// HTTP + SSE API instead of spawning a CLI subprocess per turn. Omnigent is a
// multi-harness orchestrator: its server seeds one built-in agent per native
// harness (claude-native-ui, codex-native-ui, ...) plus bundled orchestrator
// agents (polly, debby), and users register their own agents as config.yaml
// bundles. Multica surfaces that agent catalog through the model dropdown
// (see discoverOmnigentAgents), so agent.model holds an Omnigent agent name.
//
// Execution topology: an Omnigent server alone runs nothing — a turn executes
// in a runner subprocess owned by the local host daemon. `omnigent host
// --server ""` brings up both (it reuses a healthy server recorded in
// ~/.omnigent/local_server.pid), so ensureOmnigentStack spawns that once and
// then everything else is plain HTTP against http://127.0.0.1:<port>. The
// default local server runs in single-user header-auth mode, so no
// credentials are required from loopback.
//
// Turn flow: resolve agent -> pick the online local host -> create (or
// resume) a session bound to opts.Cwd -> subscribe to the session SSE stream
// -> post the user message -> map streamed events onto Messages until the
// session settles back to idle/failed. Session ids (conv_...) round-trip
// through ExecOptions.ResumeSessionID / Result.SessionID so follow-up turns
// continue the same Omnigent conversation.
type omnigentBackend struct {
	cfg Config
}

// ── Local stack discovery / bring-up ──

// omnigentServerInfo mirrors `omnigent server status --json`.
type omnigentServerInfo struct {
	Running        bool   `json:"running"`
	Port           int    `json:"port"`
	URL            string `json:"url"`
	DaemonAttached bool   `json:"daemon_attached"`
}

// omnigentEnsureMu serializes stack bring-up so concurrent tasks don't race
// to spawn multiple `omnigent host` daemons.
var omnigentEnsureMu sync.Mutex

const omnigentStackStartTimeout = 90 * time.Second

func omnigentServerStatus(ctx context.Context, execPath string) (omnigentServerInfo, error) {
	statusCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(statusCtx, execPath, "server", "status", "--json")
	hideAgentWindow(cmd)
	cmd.WaitDelay = 2 * time.Second
	out, err := cmd.Output()
	if err != nil {
		return omnigentServerInfo{}, fmt.Errorf("omnigent server status: %w", err)
	}
	var info omnigentServerInfo
	if err := json.Unmarshal(out, &info); err != nil {
		return omnigentServerInfo{}, fmt.Errorf("parse omnigent server status: %w", err)
	}
	return info, nil
}

// ensureOmnigentStack returns the local Omnigent server base URL, bringing up
// the server (and, when needHost is true, the local host daemon that owns
// runner subprocesses) if they are not already running. The spawned
// `omnigent host` daemon is deliberately detached from ctx: it is the user's
// persistent local Omnigent stack (the CLI's own `omnigent run` leaves the
// same daemon behind), not a per-task child.
func ensureOmnigentStack(ctx context.Context, execPath string, logger *slog.Logger, needHost bool) (string, error) {
	ready := func(info omnigentServerInfo) bool {
		return info.Running && info.URL != "" && (!needHost || info.DaemonAttached)
	}

	if info, err := omnigentServerStatus(ctx, execPath); err == nil && ready(info) {
		return info.URL, nil
	}

	omnigentEnsureMu.Lock()
	defer omnigentEnsureMu.Unlock()

	// Re-check under the lock — another task may have finished bring-up.
	if info, err := omnigentServerStatus(ctx, execPath); err == nil && ready(info) {
		return info.URL, nil
	}

	// `omnigent host --server ""` selects local mode: it starts (or reuses)
	// the persistent local server and connects this machine as a host, which
	// is exactly the pair a session needs to execute. It is a foreground
	// daemon, so it is started detached and left running.
	cmd := exec.Command(execPath, "host", "--server", "", "--non-interactive")
	hideAgentWindow(cmd)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		return "", fmt.Errorf("start omnigent host daemon: %w", err)
	}
	logger.Info("omnigent: local stack starting", "pid", cmd.Process.Pid)
	_ = cmd.Process.Release()

	deadline := time.Now().Add(omnigentStackStartTimeout)
	for {
		if info, err := omnigentServerStatus(ctx, execPath); err == nil && ready(info) {
			return info.URL, nil
		}
		if time.Now().After(deadline) {
			return "", fmt.Errorf("omnigent local stack did not become ready within %s (run `omnigent server status` to inspect)", omnigentStackStartTimeout)
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
}

// ── HTTP helpers ──

// omnigentHTTPClient serves plain request/response calls. SSE streaming uses
// its own client without a global timeout (omnigentStreamClient).
var omnigentHTTPClient = &http.Client{Timeout: 60 * time.Second}

var omnigentStreamClient = &http.Client{}

func omnigentDoJSON(ctx context.Context, method, url string, body any, out any) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, reader)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := omnigentHTTPClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("omnigent %s %s: %s: %s", method, req.URL.Path, resp.Status, strings.TrimSpace(string(data)))
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			return fmt.Errorf("omnigent %s %s: parse response: %w", method, req.URL.Path, err)
		}
	}
	return nil
}

// ── Agent catalog (feeds the model dropdown) ──

type omnigentAgentObject struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
	Harness     string `json:"harness"`
	Builtin     bool   `json:"builtin"`
}

func omnigentListAgents(ctx context.Context, baseURL string) ([]omnigentAgentObject, error) {
	var page struct {
		Data []omnigentAgentObject `json:"data"`
	}
	if err := omnigentDoJSON(ctx, http.MethodGet, baseURL+"/v1/agents?limit=1000&order=asc", nil, &page); err != nil {
		return nil, err
	}
	return page.Data, nil
}

// omnigentDefaultAgentName is the seeded agent used when agent.model is
// empty. The claude-native-ui built-in exists on every Omnigent server.
const omnigentDefaultAgentName = "claude-native-ui"

// discoverOmnigentAgents lists the Omnigent server's registered agents —
// built-in native harness wrappers (claude-native-ui, ...), the bundled
// orchestrators (polly, debby), and any user-registered custom agents — as
// Model entries, so the standard model dropdown doubles as Omnigent's
// agent/harness picker. The agent NAME is used as the model id (names are
// unique server-side and survive re-registration; ids can change).
func discoverOmnigentAgents(ctx context.Context, executablePath string) ([]Model, error) {
	if executablePath == "" {
		executablePath = "omnigent"
	}
	if _, err := exec.LookPath(executablePath); err != nil {
		return []Model{}, nil
	}
	// The catalog lives in the server, so discovery needs it running; the
	// host daemon is not required just to list agents.
	baseURL, err := ensureOmnigentStack(ctx, executablePath, slog.Default(), false)
	if err != nil {
		return []Model{}, nil
	}
	agents, err := omnigentListAgents(ctx, baseURL)
	if err != nil {
		return []Model{}, nil
	}
	return omnigentAgentsToModels(agents), nil
}

// omnigentAgentsToModels maps the agent catalog onto dropdown Model entries:
// native CLI wrappers first, then custom agents, alphabetical within each
// group, so the dropdown reads like the Omnigent picker.
func omnigentAgentsToModels(agents []omnigentAgentObject) []Model {
	models := make([]Model, 0, len(agents))
	for _, a := range agents {
		if a.Name == "" {
			continue
		}
		models = append(models, Model{
			ID:       a.Name,
			Label:    omnigentAgentLabel(a.Name),
			Provider: omnigentAgentGroup(a),
			Default:  a.Name == omnigentDefaultAgentName,
		})
	}
	sort.SliceStable(models, func(i, j int) bool {
		if models[i].Provider != models[j].Provider {
			return models[i].Provider == "native"
		}
		return models[i].ID < models[j].ID
	})
	return models
}

// omnigentAgentLabel prettifies seeded wrapper names ("claude-native-ui" ->
// "Claude (native CLI)") and title-cases plain agent names ("polly" ->
// "Polly"). Unknown shapes pass through unchanged.
func omnigentAgentLabel(name string) string {
	if base, ok := strings.CutSuffix(name, "-native-ui"); ok {
		return titleWord(base) + " (native CLI)"
	}
	if !strings.ContainsAny(name, "-_./") {
		return titleWord(name)
	}
	return name
}

func titleWord(s string) string {
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + s[1:]
}

func omnigentAgentGroup(a omnigentAgentObject) string {
	if strings.HasSuffix(a.Name, "-native-ui") {
		return "native"
	}
	return "custom"
}

// ── Execution ──

type omnigentHost struct {
	HostID          string  `json:"host_id"`
	Name            string  `json:"name"`
	Status          string  `json:"status"`
	SandboxProvider *string `json:"sandbox_provider"`
}

// omnigentPickHost returns the id of an online, user-connectable (non
// sandbox-managed) host. The local host daemon registers one on attach, but
// registration can trail `daemon_attached` by a beat, so poll briefly.
func omnigentPickHost(ctx context.Context, baseURL string) (string, error) {
	deadline := time.Now().Add(30 * time.Second)
	for {
		var resp struct {
			Hosts []omnigentHost `json:"hosts"`
		}
		if err := omnigentDoJSON(ctx, http.MethodGet, baseURL+"/v1/hosts", nil, &resp); err == nil {
			for _, h := range resp.Hosts {
				if h.Status == "online" && h.SandboxProvider == nil {
					return h.HostID, nil
				}
			}
		}
		if time.Now().After(deadline) {
			return "", fmt.Errorf("no online omnigent host; is `omnigent host` connected to the local server?")
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
}

func omnigentResolveAgentID(ctx context.Context, baseURL, model string) (string, error) {
	agents, err := omnigentListAgents(ctx, baseURL)
	if err != nil {
		return "", err
	}
	target := strings.TrimSpace(model)
	if target == "" {
		target = omnigentDefaultAgentName
	}
	names := make([]string, 0, len(agents))
	for _, a := range agents {
		if a.Name == target || a.ID == target {
			return a.ID, nil
		}
		names = append(names, a.Name)
	}
	return "", fmt.Errorf("omnigent agent %q not found on the local server; available: %s", target, strings.Join(names, ", "))
}

func (b *omnigentBackend) Execute(ctx context.Context, prompt string, opts ExecOptions) (*Session, error) {
	execPath := b.cfg.ExecutablePath
	if execPath == "" {
		execPath = "omnigent"
	}
	if _, err := exec.LookPath(execPath); err != nil {
		return nil, fmt.Errorf("omnigent executable not found at %q: %w", execPath, err)
	}

	baseURL, err := ensureOmnigentStack(ctx, execPath, b.cfg.Logger, true)
	if err != nil {
		return nil, err
	}

	sessionID := opts.ResumeSessionID
	if sessionID == "" {
		agentID, err := omnigentResolveAgentID(ctx, baseURL, opts.Model)
		if err != nil {
			return nil, err
		}
		hostID, err := omnigentPickHost(ctx, baseURL)
		if err != nil {
			return nil, err
		}
		if opts.Cwd == "" {
			return nil, fmt.Errorf("omnigent: a working directory is required to create a session")
		}
		create := map[string]any{
			"agent_id":  agentID,
			"host_id":   hostID,
			"workspace": opts.Cwd,
		}
		if opts.ThreadName != "" {
			create["title"] = opts.ThreadName
		}
		var created struct {
			ID        string `json:"id"`
			SessionID string `json:"session_id"`
		}
		if err := omnigentDoJSON(ctx, http.MethodPost, baseURL+"/v1/sessions", create, &created); err != nil {
			return nil, err
		}
		sessionID = created.ID
		if sessionID == "" {
			sessionID = created.SessionID
		}
		if sessionID == "" {
			return nil, fmt.Errorf("omnigent: session create returned no id")
		}
	}

	runCtx, cancel := runContext(ctx, opts.Timeout)

	// Subscribe BEFORE posting the message — the stream is live-tail only
	// (no replay), so events between post and subscribe would be lost.
	streamReq, err := http.NewRequestWithContext(runCtx, http.MethodGet, baseURL+"/v1/sessions/"+sessionID+"/stream", nil)
	if err != nil {
		cancel()
		return nil, err
	}
	streamResp, err := omnigentStreamClient.Do(streamReq)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("omnigent: open session stream: %w", err)
	}
	if streamResp.StatusCode != http.StatusOK {
		streamResp.Body.Close()
		cancel()
		return nil, fmt.Errorf("omnigent: open session stream: %s", streamResp.Status)
	}

	message := map[string]any{
		"type": "message",
		"data": map[string]any{
			"role":    "user",
			"content": []map[string]any{{"type": "input_text", "text": prompt}},
		},
	}
	if err := omnigentDoJSON(ctx, http.MethodPost, baseURL+"/v1/sessions/"+sessionID+"/events", message, nil); err != nil {
		streamResp.Body.Close()
		cancel()
		return nil, err
	}

	msgCh := make(chan Message, 256)
	resCh := make(chan Result, 1)

	go func() {
		// Force the SSE read loop to return when the run is cancelled or
		// times out; interrupt the server-side turn best-effort first.
		<-runCtx.Done()
		interruptCtx, interruptCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer interruptCancel()
		_ = omnigentDoJSON(interruptCtx, http.MethodPost, baseURL+"/v1/sessions/"+sessionID+"/events",
			map[string]any{"type": "interrupt", "data": map[string]any{}}, nil)
		streamResp.Body.Close()
	}()

	go b.consumeStream(runCtx, cancel, baseURL, sessionID, streamResp.Body, opts.Timeout, msgCh, resCh)

	return &Session{Messages: msgCh, Result: resCh}, nil
}

// consumeStream maps the session SSE stream onto the unified Message/Result
// model. A turn is considered finished when the session settles back to
// "idle" after showing activity (this covers both SDK harnesses, which also
// emit response.completed, and native terminal harnesses, which report
// status through the transcript forwarder), or immediately on
// "failed" / response.failed.
func (b *omnigentBackend) consumeStream(runCtx context.Context, cancel context.CancelFunc, baseURL, sessionID string, body io.ReadCloser, timeout time.Duration, msgCh chan Message, resCh chan Result) {
	defer cancel()
	defer body.Close()
	defer close(msgCh)
	defer close(resCh)

	startTime := time.Now()
	var outputs []string
	var deltaBuf strings.Builder
	usage := map[string]TokenUsage{}
	// The server can emit response.output_item.done twice for one tool call —
	// once with item.status=in_progress when the call is issued and once with
	// status=completed when its output lands (distinct item ids, same call_id).
	// seenCalls dedupes both function_call and function_call_output emissions
	// by "<kind>:<call_id>" so the UI transcript shows each call once.
	seenCalls := map[string]bool{}
	sawActivity := false
	finalStatus := ""
	finalError := ""

	trySend(msgCh, Message{Type: MessageStatus, Status: "running", SessionID: sessionID})

	finish := func(status, errMsg string) {
		if finalStatus == "" {
			finalStatus = status
			finalError = errMsg
		}
	}

	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 1024*1024), 10*1024*1024)
	for scanner.Scan() && finalStatus == "" {
		line := strings.TrimSpace(scanner.Text())
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		payload := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if payload == "" {
			continue
		}
		if payload == "[DONE]" {
			break
		}
		var ev omnigentStreamEvent
		if err := json.Unmarshal([]byte(payload), &ev); err != nil {
			continue
		}

		switch {
		case ev.Type == "session.status":
			// The live server emits `status` at the top level of the event
			// (ServerStreamEvent); the nested data.status form is kept as a
			// fallback for older payloads. Reading only data.status left every
			// turn stuck in "running" because idle/failed were never seen.
			status := ev.Status
			if status == "" {
				status = ev.Data.Status
			}
			trySend(msgCh, Message{Type: MessageStatus, Status: status, SessionID: sessionID})
			switch status {
			case "running", "waiting":
				sawActivity = true
			case "failed":
				finish("failed", "omnigent session failed")
			case "idle":
				if sawActivity {
					finish("completed", "")
				}
			}
		case ev.Type == "response.created" || ev.Type == "response.in_progress" || ev.Type == "session.input.consumed":
			sawActivity = true
		case ev.Type == "response.output_text.delta":
			deltaBuf.WriteString(ev.Delta)
		case ev.Type == "response.output_item.done" || ev.Type == "response.output_item.added":
			if ev.Type == "response.output_item.done" {
				sawActivity = true
			}
			b.emitItem(ev, msgCh, &outputs, &deltaBuf, seenCalls)
		case ev.Type == "response.failed" || ev.Type == "response.error":
			sawActivity = true
			errMsg := ev.ErrorMessage()
			if errMsg == "" {
				errMsg = "omnigent response failed"
			}
			finish("failed", errMsg)
		case ev.Type == "session.usage":
			// Live shape: top-level usage_by_model keyed by real model id.
			// The nested data.* form is kept as a fallback for older payloads.
			if len(ev.UsageByModel) > 0 {
				for model, u := range ev.UsageByModel {
					if model == "" {
						model = "omnigent"
					}
					usage[model] = TokenUsage{
						InputTokens:  u.InputTokens,
						OutputTokens: u.OutputTokens,
					}
				}
			} else {
				model := ev.Data.Model
				if model == "" {
					model = "omnigent"
				}
				usage[model] = TokenUsage{
					InputTokens:  ev.Data.CumulativeInputTokens,
					OutputTokens: ev.Data.CumulativeOutputTokens,
				}
			}
		case ev.Type == "response.elicitation_request":
			// Headless runs cannot surface approval prompts; accept them,
			// matching the skip-permissions posture every other backend runs
			// with under the daemon.
			target := ev.Params.TargetSessionID
			if target == "" {
				target = sessionID
			}
			resolveCtx, resolveCancel := context.WithTimeout(context.Background(), 15*time.Second)
			err := omnigentDoJSON(resolveCtx, http.MethodPost,
				baseURL+"/v1/sessions/"+target+"/elicitations/"+ev.ElicitationID+"/resolve",
				map[string]any{"action": "accept"}, nil)
			resolveCancel()
			if err != nil {
				b.cfg.Logger.Warn("omnigent: auto-accept elicitation failed", "error", err)
			}
			trySend(msgCh, Message{Type: MessageLog, Level: "info", Content: "auto-accepted omnigent approval request"})
		}
	}

	if finalStatus == "" {
		switch {
		case runCtx.Err() == context.DeadlineExceeded:
			finish("timeout", fmt.Sprintf("omnigent session timed out after %s", timeout))
		case runCtx.Err() == context.Canceled:
			finish("aborted", "execution cancelled")
		default:
			finish("failed", "omnigent session stream closed before the turn finished")
		}
	}

	// A native-harness turn can stream deltas without a final message item;
	// don't lose that text.
	if tail := strings.TrimSpace(deltaBuf.String()); tail != "" {
		outputs = append(outputs, tail)
		trySend(msgCh, Message{Type: MessageText, Content: tail})
	}

	resCh <- Result{
		Status:     finalStatus,
		Output:     strings.Join(outputs, "\n\n"),
		Error:      finalError,
		DurationMs: time.Since(startTime).Milliseconds(),
		SessionID:  sessionID,
		Usage:      usage,
	}
}

// emitItem maps a completed conversation item (OpenAI Responses shape) onto
// the unified Message model. seenCalls dedupes tool-call items the server
// reports more than once (in_progress + completed done-events per call_id).
func (b *omnigentBackend) emitItem(ev omnigentStreamEvent, msgCh chan Message, outputs *[]string, deltaBuf *strings.Builder, seenCalls map[string]bool) {
	item := ev.Item
	if item == nil {
		return
	}
	switch item.Type {
	case "message":
		if item.Role != "assistant" || ev.Type != "response.output_item.done" {
			return
		}
		// Defensive mirror of the function_call double-done: only treat a
		// message item as final when it isn't still marked in_progress —
		// deltas cover the streaming, and the completed event follows.
		if item.Status == "in_progress" {
			return
		}
		var parts []string
		for _, c := range item.Content {
			if c.Type == "output_text" && strings.TrimSpace(c.Text) != "" {
				parts = append(parts, c.Text)
			}
		}
		text := strings.Join(parts, "\n")
		if strings.TrimSpace(text) == "" {
			return
		}
		*outputs = append(*outputs, text)
		// The item text is the authoritative form of what the deltas
		// streamed; drop the buffer so it isn't double-counted.
		deltaBuf.Reset()
		trySend(msgCh, Message{Type: MessageText, Content: text})
	case "function_call":
		if ev.Type != "response.output_item.done" {
			return
		}
		if item.CallID != "" {
			key := "call:" + item.CallID
			if seenCalls[key] {
				return
			}
			seenCalls[key] = true
		}
		input := map[string]any{}
		if item.Arguments != "" {
			_ = json.Unmarshal([]byte(item.Arguments), &input)
		}
		trySend(msgCh, Message{Type: MessageToolUse, Tool: item.Name, CallID: item.CallID, Input: input})
	case "function_call_output":
		if ev.Type != "response.output_item.done" {
			return
		}
		if item.CallID != "" {
			key := "output:" + item.CallID
			if seenCalls[key] {
				return
			}
			seenCalls[key] = true
		}
		trySend(msgCh, Message{Type: MessageToolResult, CallID: item.CallID, Output: item.Output})
	}
}

// omnigentStreamEvent is a permissive union of the SSE event fields this
// backend consumes. Canonical shapes: openapi.json ServerStreamEvent. The
// live server puts session.status's `status` and session.usage's
// `usage_by_model` at the top level of the event; the nested `data` block is
// kept as a fallback for older payload shapes.
type omnigentStreamEvent struct {
	Type string `json:"type"`
	// Status is session.status's top-level payload ("running"/"idle"/...).
	Status string `json:"status"`
	// UsageByModel is session.usage's top-level cumulative usage keyed by
	// real model id (e.g. "claude-fable-5").
	UsageByModel map[string]omnigentModelUsage `json:"usage_by_model"`
	Data         struct {
		Status                 string `json:"status"`
		Model                  string `json:"model"`
		CumulativeInputTokens  int64  `json:"cumulative_input_tokens"`
		CumulativeOutputTokens int64  `json:"cumulative_output_tokens"`
	} `json:"data"`
	Delta         string              `json:"delta"`
	Item          *omnigentStreamItem `json:"item"`
	ElicitationID string              `json:"elicitation_id"`
	Params        struct {
		TargetSessionID string `json:"target_session_id"`
	} `json:"params"`
	Error   json.RawMessage `json:"error"`
	Message string          `json:"message"`
}

// ErrorMessage extracts a human-readable error from response.failed /
// response.error events, whose payloads vary between a string and an object.
func (e *omnigentStreamEvent) ErrorMessage() string {
	if e.Message != "" {
		return e.Message
	}
	if len(e.Error) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(e.Error, &s); err == nil {
		return s
	}
	var obj struct {
		Message string `json:"message"`
	}
	if err := json.Unmarshal(e.Error, &obj); err == nil && obj.Message != "" {
		return obj.Message
	}
	return strings.TrimSpace(string(e.Error))
}

type omnigentStreamItem struct {
	Type      string `json:"type"`
	Status    string `json:"status"`
	Role      string `json:"role"`
	Name      string `json:"name"`
	CallID    string `json:"call_id"`
	Arguments string `json:"arguments"`
	Output    string `json:"output"`
	Content   []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"content"`
}

// omnigentModelUsage is one entry in session.usage's usage_by_model map.
// Token fields may be null upstream; they unmarshal to 0.
type omnigentModelUsage struct {
	InputTokens  int64 `json:"input_tokens"`
	OutputTokens int64 `json:"output_tokens"`
}
