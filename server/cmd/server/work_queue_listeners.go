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
