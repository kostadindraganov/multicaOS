"use client";

import { useState } from "react";
import { ListChecks, Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { queueListOptions } from "@multica/core/queues";
import { useWorkspaceId } from "@multica/core/hooks";
import { useWorkspacePaths } from "@multica/core/paths";
import { useActorName } from "@multica/core/workspace/hooks";
import type { WorkQueue, QueueStatus } from "@multica/core/types";
import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import {
  ListGrid,
  ListGridBody,
  ListGridCell,
  ListGridHeader,
  ListGridHeaderCell,
  ListGridRow,
} from "@multica/ui/components/ui/list-grid";
import { Skeleton } from "@multica/ui/components/ui/skeleton";
import { useRowLink } from "../../navigation";
import { ActorAvatar } from "../../common/actor-avatar";
import { PageHeader } from "../../layout/page-header";
import { QueueDialog } from "./queue-dialog";
import { useT } from "../../i18n";

const GRID_COLS = "grid-cols-[0.75rem_minmax(160px,1fr)_6rem_10rem_8rem_0.75rem]";

const STATUS_VARIANT: Record<QueueStatus, "default" | "secondary" | "outline"> = {
  idle: "outline",
  scheduled: "secondary",
  running: "default",
  paused: "outline",
};

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
      </ListGridHeader>
      {Array.from({ length: 4 }).map((_, i) => (
        <ListGridRow key={i} className="hover:bg-transparent">
          <ListGridCell>
            <Skeleton className="h-3.5 w-40 max-w-full" />
          </ListGridCell>
          <ListGridCell>
            <Skeleton className="h-5 w-16" />
          </ListGridCell>
          <ListGridCell className="gap-1.5">
            <Skeleton className="size-5 rounded-full" />
            <Skeleton className="h-3 w-12" />
          </ListGridCell>
          <ListGridCell>
            <Skeleton className="h-3 w-16" />
          </ListGridCell>
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

  const [createOpen, setCreateOpen] = useState(false);

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
        <div className="min-h-0 flex-1 overflow-auto @container">
          <ListGrid className={GRID_COLS}>
            <ListGridHeader>
              <ListGridHeaderCell>{t(($) => $.page.table.name)}</ListGridHeaderCell>
              <ListGridHeaderCell>{t(($) => $.page.table.status)}</ListGridHeaderCell>
              <ListGridHeaderCell>{t(($) => $.page.table.agent)}</ListGridHeaderCell>
              <ListGridHeaderCell>{t(($) => $.page.table.next_run)}</ListGridHeaderCell>
            </ListGridHeader>
            <ListGridBody>
              {queues.map((queue) => (
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
                  <ListGridCell className="gap-1.5">
                    <AgentCell queue={queue} />
                  </ListGridCell>
                  <ListGridCell>
                    <NextRunCell queue={queue} />
                  </ListGridCell>
                </ListGridRow>
              ))}
            </ListGridBody>
          </ListGrid>
        </div>
      )}

      {createOpen && (
        <QueueDialog mode="create" open={createOpen} onOpenChange={setCreateOpen} />
      )}
    </div>
  );
}
