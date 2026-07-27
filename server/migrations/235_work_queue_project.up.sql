-- Queues v2: optional project association on work queues. Filtering happens
-- client-side; the column exists so a queue can belong to a project and
-- survive project deletion as an unassociated queue (SET NULL).
ALTER TABLE work_queue ADD COLUMN project_id UUID REFERENCES project(id) ON DELETE SET NULL;

CREATE INDEX idx_work_queue_workspace_project ON work_queue(workspace_id, project_id);
