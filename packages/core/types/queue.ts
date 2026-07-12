export type QueueStatus = "idle" | "scheduled" | "running" | "paused";
export type QueueItemKind = "prompt" | "issue";
export type QueueItemStatus = "pending" | "running" | "completed" | "failed";

export interface WorkQueue {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  default_agent_id: string | null;
  status: QueueStatus;
  start_at: string | null;
  item_delay_seconds: number;
  cron_expression: string | null;
  timezone: string | null;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkQueueItem {
  id: string;
  queue_id: string;
  seq: number;
  kind: QueueItemKind;
  title: string | null;
  body: string | null;
  issue_id: string | null;
  agent_id: string | null;
  status: QueueItemStatus;
  task_id: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface CreateQueueRequest {
  name: string;
  description?: string;
  default_agent_id?: string;
  item_delay_seconds?: number;
  cron_expression?: string;
  timezone?: string;
}
export type UpdateQueueRequest = Partial<CreateQueueRequest>;

export interface AddQueueItemsRequest {
  items: Array<{
    kind: QueueItemKind;
    title?: string;
    body?: string;
    issue_id?: string;
    agent_id?: string;
  }>;
}

export interface ListQueuesResponse {
  queues: WorkQueue[];
  total: number;
}
export interface GetQueueResponse {
  queue: WorkQueue;
  items: WorkQueueItem[];
}
