-- Dropping the column also drops idx_work_queue_workspace_project.
ALTER TABLE work_queue DROP COLUMN IF EXISTS project_id;
