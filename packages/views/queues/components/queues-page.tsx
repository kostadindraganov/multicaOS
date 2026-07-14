"use client";

import { useState } from "react";
import { ListChecks, MoreHorizontal, Pencil, Plus, Trash2 } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { queueListOptions } from "@multica/core/queues";
import { projectListOptions } from "@multica/core/projects/queries";
import { useWorkspaceId } from "@multica/core/hooks";
import { useWorkspacePaths } from "@multica/core/paths";
import { useActorName } from "@multica/core/workspace/hooks";
import type { WorkQueue, QueueStatus } from "@multica/core/types";
import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@multica/ui/components/ui/dropdown-menu";
import { Input } from "@multica/ui/components/ui/input";
import {
  ListGrid,
  ListGridBody,
  ListGridCell,
  ListGridHeader,
  ListGridHeaderCell,
  ListGridRow,
} from "@multica/ui/components/ui/list-grid";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@multica/ui/components/ui/select";
import { Skeleton } from "@multica/ui/components/ui/skeleton";
import { useRowLink } from "../../navigation";
import { ActorAvatar } from "../../common/actor-avatar";
import { PageHeader } from "../../layout/page-header";
import { QueueDialog } from "./queue-dialog";
import { DeleteQueueDialog } from "./delete-queue-dialog";
import { useT } from "../../i18n";

const GRID_COLS =
  "grid-cols-[0.75rem_minmax(160px,1fr)_6rem_9rem_10rem_8rem_2.5rem_0.75rem]";

const STATUS_VARIANT: Record<QueueStatus, "default" | "secondary" | "outline"> = {
  idle: "outline",
  scheduled: "secondary",
  running: "default",
  paused: "outline",
};

const STATUS_KEYS: QueueStatus[] = ["idle", "scheduled", "running", "paused"];

export function QueueStatusBadge({ status }: { status: QueueStatus }) {
  const { t } = useT("queues");
  const known = status === "idle" || status === "scheduled" || status === "running" || status === "paused";
  return (
    <Badge variant={known ? STATUS_VARIANT[status] : "outline"}>
      {known ? t(($) => $.status[status]) : status}
    </Badge>
  );
}

function NextRunCell({ queue }: { queue: WorkQueue }) {
  if (!queue.next_run_at) {
    return <span className="text-xs text-muted-foreground/40">—</span>;
  }
  return (
    <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
      {new Date(queue.next_run_at).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })}
    </span>
  );
}

function AgentCell({ queue }: { queue: WorkQueue }) {
  const { getActorName } = useActorName();
  if (!queue.default_agent_id) {
    return <span className="text-xs text-muted-foreground/40">—</span>;
  }
  return (
    <>
      <ActorAvatar actorType="agent" actorId={queue.default_agent_id} size="sm" showStatusDot />
      <span className="min-w-0 truncate text-xs text-muted-foreground">
        {getActorName("agent", queue.default_agent_id)}
      </span>
    </>
  );
}

function ProgressCell({ queue }: { queue: WorkQueue }) {
  const { t } = useT("queues");
  const c = queue.item_counts;
  const total = c ? c.pending + c.running + c.completed + c.failed : 0;
  // Older servers omit counts entirely; an empty queue has nothing to chart.
  if (!c || total === 0) {
    return <span className="text-xs text-muted-foreground/40">—</span>;
  }
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1">
      <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">
        {t(($) => $.page.progress_done, { completed: c.completed, total })}
        {c.failed > 0 && (
          <span className="text-destructive">
            {" · "}
            {t(($) => $.page.progress_failed, { count: c.failed })}
          </span>
        )}
      </span>
      <div className="h-1 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary"
          style={{ width: `${Math.round((c.completed / total) * 100)}%` }}
        />
      </div>
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <ListGrid className={GRID_COLS}>
      <ListGridHeader>
        <ListGridHeaderCell>
          <Skeleton className="h-3 w-16" />
        </ListGridHeaderCell>
        <ListGridHeaderCell>
          <Skeleton className="h-3 w-12" />
        </ListGridHeaderCell>
        <ListGridHeaderCell>
          <Skeleton className="h-3 w-14" />
        </ListGridHeaderCell>
        <ListGridHeaderCell>
          <Skeleton className="h-3 w-14" />
        </ListGridHeaderCell>
        <ListGridHeaderCell>
          <Skeleton className="h-3 w-14" />
        </ListGridHeaderCell>
        <ListGridHeaderCell />
      </ListGridHeader>
      {Array.from({ length: 4 }).map((_, i) => (
        <ListGridRow key={i} className="hover:bg-transparent">
          <ListGridCell>
            <Skeleton className="h-3.5 w-40 max-w-full" />
          </ListGridCell>
          <ListGridCell>
            <Skeleton className="h-5 w-16" />
          </ListGridCell>
          <ListGridCell>
            <Skeleton className="h-3 w-20" />
          </ListGridCell>
          <ListGridCell className="gap-1.5">
            <Skeleton className="size-5 rounded-full" />
            <Skeleton className="h-3 w-12" />
          </ListGridCell>
          <ListGridCell>
            <Skeleton className="h-3 w-16" />
          </ListGridCell>
          <ListGridCell />
        </ListGridRow>
      ))}
    </ListGrid>
  );
}

export function QueuesPage() {
  const { t } = useT("queues");
  const wsId = useWorkspaceId();
  const wsPaths = useWorkspacePaths();
  const rowLink = useRowLink();
  const { data: queues = [], isLoading } = useQuery(queueListOptions(wsId));
  const { data: projects = [] } = useQuery(projectListOptions(wsId));

  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<WorkQueue | null>(null);
  const [editTarget, setEditTarget] = useState<WorkQueue | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | QueueStatus>("all");
  const [projectFilter, setProjectFilter] = useState<string>("all");

  const filtered = queues.filter((q) => {
    if (search && !q.name.toLowerCase().includes(search.toLowerCase())) return false;
    if (statusFilter !== "all" && q.status !== statusFilter) return false;
    if (projectFilter === "none") return q.project_id == null;
    if (projectFilter !== "all") return q.project_id === projectFilter;
    return true;
  });

  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <PageHeader className="justify-between px-5">
        <div className="flex items-center gap-2">
          <ListChecks className="h-4 w-4 text-muted-foreground" />
          <h1 className="text-sm font-medium">{t(($) => $.page.title)}</h1>
          {queues.length > 0 && (
            <span className="font-mono text-xs tabular-nums text-muted-foreground/70">
              {queues.length}
            </span>
          )}
        </div>
        <Button
          size="sm"
          variant="outline"
          className="h-8 w-8 gap-1 px-0 md:w-auto md:px-2.5"
          aria-label={t(($) => $.page.new_queue)}
          onClick={() => setCreateOpen(true)}
        >
          <Plus className="h-3.5 w-3.5" />
          <span className="hidden md:inline">{t(($) => $.page.new_queue)}</span>
        </Button>
      </PageHeader>

      {isLoading ? (
        <div className="flex-1 overflow-y-auto @container">
          <LoadingSkeleton />
        </div>
      ) : queues.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-5 py-16 text-center">
          <ListChecks className="size-10 text-muted-foreground/50" />
          <p className="text-sm text-muted-foreground">{t(($) => $.page.empty_title)}</p>
          <p className="text-xs text-muted-foreground">{t(($) => $.page.empty_hint)}</p>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="size-3.5" />
            {t(($) => $.page.new_queue)}
          </Button>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2 border-b px-5 py-2">
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t(($) => $.page.search_placeholder)}
              className="h-8 w-56"
            />
            <Select
              value={statusFilter}
              onValueChange={(v) => v && setStatusFilter(v as "all" | QueueStatus)}
            >
              <SelectTrigger size="sm" className="w-36" aria-label={t(($) => $.page.table.status)}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t(($) => $.page.filter_status_all)}</SelectItem>
                {STATUS_KEYS.map((s) => (
                  <SelectItem key={s} value={s}>
                    {t(($) => $.status[s])}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={projectFilter} onValueChange={(v) => v && setProjectFilter(v)}>
              <SelectTrigger
                size="sm"
                className="w-44"
                aria-label={t(($) => $.page.filter_project_all)}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t(($) => $.page.filter_project_all)}</SelectItem>
                <SelectItem value="none">{t(($) => $.page.filter_project_none)}</SelectItem>
                {projects.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="min-h-0 flex-1 overflow-auto @container">
            {filtered.length === 0 ? (
              <p className="px-5 py-8 text-center text-sm text-muted-foreground">
                {t(($) => $.page.no_matches)}
              </p>
            ) : (
              <ListGrid className={GRID_COLS}>
                <ListGridHeader>
                  <ListGridHeaderCell>{t(($) => $.page.table.name)}</ListGridHeaderCell>
                  <ListGridHeaderCell>{t(($) => $.page.table.status)}</ListGridHeaderCell>
                  <ListGridHeaderCell>{t(($) => $.page.table.progress)}</ListGridHeaderCell>
                  <ListGridHeaderCell>{t(($) => $.page.table.agent)}</ListGridHeaderCell>
                  <ListGridHeaderCell>{t(($) => $.page.table.next_run)}</ListGridHeaderCell>
                  <ListGridHeaderCell />
                </ListGridHeader>
                <ListGridBody>
                  {filtered.map((queue) => (
                    <ListGridRow
                      key={queue.id}
                      className="cursor-pointer"
                      {...rowLink(wsPaths.queueDetail(queue.id))}
                    >
                      <ListGridCell>
                        <span className="min-w-0 truncate text-sm font-medium">{queue.name}</span>
                      </ListGridCell>
                      <ListGridCell>
                        <QueueStatusBadge status={queue.status} />
                      </ListGridCell>
                      <ListGridCell>
                        <ProgressCell queue={queue} />
                      </ListGridCell>
                      <ListGridCell className="gap-1.5">
                        <AgentCell queue={queue} />
                      </ListGridCell>
                      <ListGridCell>
                        <NextRunCell queue={queue} />
                      </ListGridCell>
                      <ListGridCell>
                        {/* Row is a link; stop the menu from navigating. */}
                        <span onClick={(e) => e.stopPropagation()}>
                          <DropdownMenu>
                            <DropdownMenuTrigger
                              aria-label={t(($) => $.page.row_menu)}
                              className="flex size-6 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
                            >
                              <MoreHorizontal className="size-3.5" />
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem onClick={() => setEditTarget(queue)}>
                                <Pencil className="size-3.5" />
                                {t(($) => $.page.edit_menu)}
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                variant="destructive"
                                onClick={() => setDeleteTarget(queue)}
                              >
                                <Trash2 className="size-3.5" />
                                {t(($) => $.delete.menu)}
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </span>
                      </ListGridCell>
                    </ListGridRow>
                  ))}
                </ListGridBody>
              </ListGrid>
            )}
          </div>
        </>
      )}

      {createOpen && (
        <QueueDialog mode="create" open={createOpen} onOpenChange={setCreateOpen} />
      )}
      {editTarget && (
        <QueueDialog
          mode="edit"
          queue={editTarget}
          open={!!editTarget}
          onOpenChange={(v) => {
            if (!v) setEditTarget(null);
          }}
        />
      )}
      {deleteTarget && (
        <DeleteQueueDialog
          queue={deleteTarget}
          open={!!deleteTarget}
          onOpenChange={(v) => {
            if (!v) setDeleteTarget(null);
          }}
        />
      )}
    </div>
  );
}
