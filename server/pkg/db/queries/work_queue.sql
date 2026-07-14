-- ============================================================
-- Work queues (tank-style ordered prompt/issue queues)
-- ============================================================

-- name: CreateWorkQueue :one
INSERT INTO work_queue (
    workspace_id, name, description, default_agent_id,
    item_delay_seconds, cron_expression, timezone, next_run_at, created_by,
    project_id
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING *;

-- name: GetWorkQueueInWorkspace :one
SELECT * FROM work_queue
WHERE id = $1 AND workspace_id = $2;

-- name: GetWorkQueue :one
SELECT * FROM work_queue
WHERE id = $1;

-- ListWorkQueues returns each queue with derived per-status item counts so
-- the list view renders progress without an N+1 item fetch per queue.
-- name: ListWorkQueues :many
SELECT sqlc.embed(work_queue),
       (count(i.id) FILTER (WHERE i.status = 'pending'))::bigint   AS pending_count,
       (count(i.id) FILTER (WHERE i.status = 'running'))::bigint   AS running_count,
       (count(i.id) FILTER (WHERE i.status = 'completed'))::bigint AS completed_count,
       (count(i.id) FILTER (WHERE i.status = 'failed'))::bigint    AS failed_count
FROM work_queue
LEFT JOIN work_queue_item i ON i.queue_id = work_queue.id
WHERE work_queue.workspace_id = $1
GROUP BY work_queue.id
ORDER BY work_queue.created_at DESC;

-- name: UpdateWorkQueue :one
UPDATE work_queue SET
    name = COALESCE(sqlc.narg('name'), name),
    description = COALESCE(sqlc.narg('description'), description),
    default_agent_id = CASE WHEN sqlc.arg('set_default_agent')::bool THEN sqlc.narg('default_agent_id') ELSE default_agent_id END,
    project_id = CASE WHEN sqlc.arg('set_project')::bool THEN sqlc.narg('project_id') ELSE project_id END,
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

-- MarkWorkQueueScheduledStarted flips a scheduled queue to running exactly
-- once: the status guard makes concurrent tick replicas no-op for the same
-- scheduled promotion.
-- name: MarkWorkQueueScheduledStarted :execrows
UPDATE work_queue SET status = 'running', start_at = NULL, updated_at = now()
WHERE id = $1 AND status = 'scheduled';

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

-- RetryWorkQueueItem re-enqueues a failed item. The status guard makes it a
-- no-op (0 rows) for anything not currently failed.
-- name: RetryWorkQueueItem :execrows
UPDATE work_queue_item SET
    status = 'pending', error = NULL, task_id = NULL,
    started_at = NULL, finished_at = NULL, updated_at = now()
WHERE id = $1 AND workspace_id = $2 AND status = 'failed';

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
