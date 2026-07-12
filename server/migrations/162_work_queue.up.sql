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
