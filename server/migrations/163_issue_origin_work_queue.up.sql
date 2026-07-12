-- Extend issue.origin_type to allow work queue "prompt" items to stamp the
-- issue they create with origin_type='work_queue' + origin_id=<work_queue_item.id>.
-- WorkQueueService.dispatchPrompt (server/internal/service/work_queue.go)
-- relies on this so the created issue links back to the queue item that
-- produced it, mirroring the autopilot / quick_create / agent_create links.
ALTER TABLE issue DROP CONSTRAINT IF EXISTS issue_origin_type_check;
ALTER TABLE issue ADD CONSTRAINT issue_origin_type_check
    CHECK (origin_type IN ('autopilot', 'quick_create', 'lark_chat', 'slack_chat', 'agent_create', 'work_queue'));
