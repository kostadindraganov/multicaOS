# Queues v2 — Delete, Project Filter, Issue Picker, Retry, List Progress

Date: 2026-07-14
Status: Approved

Builds on the work-queues feature (spec `2026-07-12-work-queues-design.md`).
Six small, independent improvements to the Queues section.

## 1. Delete queue (UI wiring only)

Backend `DELETE /workspaces/.../queues/{id}` and `useDeleteQueue()` already
exist; there is no UI entry point.

- **Queues list page** (`packages/views/queues/components/queues-page.tsx`):
  add a `⋯` dropdown menu per queue row with a Delete action.
- **Queue detail page** (`queue-detail-page.tsx`): add Delete to a header menu.
- Both open an AlertDialog that names the queue. When `queue.status ===
  "running"`, add a warning line: the in-flight task keeps running; the queue
  and its items are removed.
- Flow rule (repo CLAUDE.md): await the server mutation, then navigate back to
  the queues list (detail page) — never optimistically remove the queue from
  cache.

## 2. Project association + project filter

Queues are currently workspace-scoped with no project link.

- **Migration** `191_work_queue_project.up.sql` / `.down.sql`:
  `ALTER TABLE work_queue ADD COLUMN project_id UUID REFERENCES project(id)
  ON DELETE SET NULL;` plus an index on `(workspace_id, project_id)`.
- **sqlc**: update `CreateWorkQueue`, `UpdateWorkQueue`, `ListWorkQueues`,
  `GetWorkQueue` in `server/pkg/db/queries/work_queue.sql`; run `make sqlc`.
- **Handlers** (`server/internal/handler/work_queue.go`): accept optional
  `project_id` on create and update. Validate the project exists in the
  workspace (same pattern as the existing `agent_id` validation); empty string
  / null clears it. Include `project_id` in queue JSON responses.
- **Core**: add `project_id: string | null` to `WorkQueue` type and
  `WorkQueueSchema` (`z.string().nullable().optional()` — older servers omit
  it). Pass through create/update mutation payloads.
- **Queue dialog** (`queue-dialog.tsx`): optional project select using the
  existing projects query from `packages/core/projects`.
- **Queues list page**: project filter dropdown. Filtering is client-side
  (queue lists are small); "All projects" default. Queues with no project
  appear under a "No project" option.

## 3. Issue picker: preload + multi-select (queues only)

`packages/views/modals/issue-picker-modal.tsx` is shared by 4 flows and is
search-only (empty until you type). Other flows must not change behavior.

- New optional props: `preloadAll?: boolean`, `multiple?: boolean`,
  `onSelectMany?: (issues: Issue[]) => void`. Defaults keep today's behavior.
- `preloadAll`: when the dialog opens with an empty query, fetch
  `api.listIssues({ limit: 50 })` and render results immediately; typing
  switches to the existing debounced `searchIssues` path. `excludeIds`
  filtering applies to both paths.
- `multiple`: rows toggle a checkbox instead of closing on select; a footer
  button "Add N issues" calls `onSelectMany` and closes.
- Queue composer (`AddItemComposer` in `queue-detail-page.tsx`) passes
  `preloadAll` and `multiple`, bulk-adding via the existing
  `POST /queues/{id}/items` array endpoint (`useAddQueueItems`).

## 4. Retry failed item

A failed item is currently a dead end (delete + re-add).

- **Endpoint** `POST /queues/{id}/items/{itemId}/retry`:
  `UPDATE work_queue_item SET status = 'pending', error = NULL, task_id =
  NULL, started_at = NULL, finished_at = NULL WHERE id = $1 AND workspace_id
  = $2 AND status = 'failed'`. Zero rows → 400 "item is not failed" (mirrors
  the existing "item is not pending" guard). Publishes the same
  work-queue-updated bus event as other item writes; existing WS invalidation
  refreshes the UI.
- **Core**: `useRetryQueueItem()` mutation + `api.retryQueueItem`.
- **UI**: retry icon button on failed item rows in the detail page. No
  auto-restart: if the queue drained to `idle`, the user presses Start again.

## 5. Progress on the queues list page

The list endpoint returns bare queues; item counts exist only on the detail
endpoint.

- **SQL**: extend `ListWorkQueues` with a LEFT JOIN aggregate producing
  pending/running/completed/failed counts per queue (reuse the count shape the
  detail handler already builds).
- **API**: include a `counts` object per queue in the list response. Schema:
  optional with per-field `default(0)` so older servers degrade gracefully.
- **UI**: each list row shows `completed/total done` plus a thin progress bar;
  failed count rendered in red when > 0. Semantic tokens only, no hardcoded
  colors.

## 6. Search + status filter on the list page

- Search input filtering queues by name (case-insensitive substring) and a
  status filter (idle / scheduled / running / paused), both client-side.
- Composes with the project filter from §2 (AND semantics).

## Testing

| Layer | Tests |
| --- | --- |
| Go handler | retry endpoint (failed→pending, non-failed → 400), `project_id` validation on create/update, list counts aggregate |
| Core | schema test: `project_id` + `counts` optional/defaulted (malformed-response case) |
| Views | queues-page: search/status/project filtering, delete confirm flow; picker: preload renders without typing, multi-select returns N issues; detail page: retry button visibility on failed items |

## Out of scope (follow-ups)

- Stuck-`running` reaper (from the work-queues merge review follow-ups).
- Queue duplication, templates, completion notifications, drag-drop from the
  issues list.
