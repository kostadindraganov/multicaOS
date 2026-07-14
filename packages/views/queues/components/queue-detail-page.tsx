"use client";

import { useEffect, useState } from "react";
import {
  DndContext,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Bot,
  GripVertical,
  Link2,
  ListChecks,
  Loader2,
  MessageSquare,
  Pause,
  Play,
  Plus,
  RotateCcw,
  RotateCw,
  Trash2,
} from "lucide-react";
import {
  queueDetailOptions,
  useAddQueueItems,
  useClearFinishedQueueItems,
  useDeleteQueueItem,
  usePauseQueue,
  useResumeQueue,
  useReorderQueueItems,
  useRetryQueueItem,
  useStartQueue,
  useUpdateQueueItem,
} from "@multica/core/queues";
import { useWorkspaceId } from "@multica/core/hooks";
import { useWorkspacePaths } from "@multica/core/paths";
import { useActorName } from "@multica/core/workspace/hooks";
import type { Issue, QueueItemStatus, WorkQueueItem } from "@multica/core/types";
import { Badge } from "@multica/ui/components/ui/badge";
import { Button } from "@multica/ui/components/ui/button";
import { Input } from "@multica/ui/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@multica/ui/components/ui/popover";
import { Skeleton } from "@multica/ui/components/ui/skeleton";
import { Textarea } from "@multica/ui/components/ui/textarea";
import { cn } from "@multica/ui/lib/utils";
import { AppLink, useNavigation } from "../../navigation";
import { ActorAvatar } from "../../common/actor-avatar";
import { BreadcrumbHeader } from "../../layout/breadcrumb-header";
import { AgentPicker } from "../../autopilots/components/pickers/agent-picker";
import { IssuePickerModal } from "../../modals/issue-picker-modal";
import { QueueStatusBadge } from "./queues-page";
import { DeleteQueueDialog } from "./delete-queue-dialog";
import { useT } from "../../i18n";

const ITEM_STATUS_VARIANT: Record<QueueItemStatus, "default" | "secondary" | "outline" | "destructive"> = {
  pending: "outline",
  running: "default",
  completed: "secondary",
  failed: "destructive",
};

function ItemStatusBadge({ status }: { status: QueueItemStatus }) {
  const { t } = useT("queues");
  const known =
    status === "pending" || status === "running" || status === "completed" || status === "failed";
  return (
    <Badge variant={known ? ITEM_STATUS_VARIANT[status] : "outline"}>
      {known ? t(($) => $.item_status[status]) : status}
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// Item row — dnd-kit sortable, same useSortable + CSS.Transform wiring as
// packages/views/issues/components/list-row.tsx's DraggableListRow.
// ---------------------------------------------------------------------------

function ItemRow({
  item,
  queueId,
}: {
  item: WorkQueueItem;
  queueId: string;
}) {
  const { t } = useT("queues");
  const wsPaths = useWorkspacePaths();
  const { getActorName } = useActorName();
  const updateItem = useUpdateQueueItem();
  const deleteItem = useDeleteQueueItem();
  const retryItem = useRetryQueueItem();

  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: item.id,
  });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  const canDelete = item.status !== "running";
  const isPending = item.status === "pending";

  const handleAgentChange = (agentId: string) => {
    updateItem.mutate(
      { id: queueId, itemId: item.id, agent_id: agentId },
      {
        onError: (err) =>
          toast.error(
            err instanceof Error && err.message
              ? err.message
              : t(($) => $.detail.toast_item_agent_update_failed),
          ),
      },
    );
  };

  const handleDelete = () => {
    deleteItem.mutate(
      { id: queueId, itemId: item.id },
      {
        onSuccess: () => toast.success(t(($) => $.detail.toast_item_deleted)),
        onError: (err) =>
          toast.error(
            err instanceof Error && err.message
              ? err.message
              : t(($) => $.detail.toast_item_delete_failed),
          ),
      },
    );
  };

  const handleRetry = () => {
    retryItem.mutate(
      { id: queueId, itemId: item.id },
      {
        onSuccess: () => toast.success(t(($) => $.detail.toast_item_retried)),
        onError: (err) =>
          toast.error(
            err instanceof Error && err.message
              ? err.message
              : t(($) => $.detail.toast_item_retry_failed),
          ),
      },
    );
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={cn(
        "group flex items-start gap-2.5 border-b px-4 py-2.5",
        isDragging && "opacity-50",
      )}
    >
      <button
        type="button"
        aria-label={t(($) => $.detail.drag_handle)}
        className="mt-1 flex shrink-0 cursor-grab items-center text-muted-foreground/50 hover:text-muted-foreground"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="size-3.5" />
      </button>

      <span className="mt-1 shrink-0 text-xs tabular-nums text-muted-foreground/60">{item.seq}</span>

      <span className="mt-0.5 shrink-0 text-muted-foreground">
        {item.kind === "issue" ? <Link2 className="size-3.5" /> : <MessageSquare className="size-3.5" />}
      </span>

      <div className="min-w-0 flex-1 space-y-0.5">
        {item.kind === "issue" && item.issue_id ? (
          <AppLink
            href={wsPaths.issueDetail(item.issue_id)}
            className="block truncate text-sm font-medium text-foreground hover:underline"
          >
            {item.title ?? item.issue_id}
          </AppLink>
        ) : (
          <span className="block truncate text-sm font-medium">
            {item.title ?? t(($) => $.detail.untitled_item)}
          </span>
        )}
        {item.error && (
          <span className="block truncate text-xs text-destructive">{item.error}</span>
        )}
      </div>

      <div className="mt-0.5 flex shrink-0 items-center gap-1.5">
        {isPending ? (
          <AgentPicker
            assignee={item.agent_id ? { type: "agent", id: item.agent_id } : null}
            onChange={(next) => {
              if (next.type === "agent") handleAgentChange(next.id);
            }}
            align="end"
          />
        ) : item.agent_id ? (
          <span className="flex items-center gap-1.5">
            <ActorAvatar actorType="agent" actorId={item.agent_id} size="sm" />
            <span className="text-xs text-muted-foreground">{getActorName("agent", item.agent_id)}</span>
          </span>
        ) : (
          <Bot className="size-3.5 text-muted-foreground/40" />
        )}
      </div>

      <div className="mt-0.5 shrink-0">
        <ItemStatusBadge status={item.status} />
      </div>

      <div className="mt-0.5 flex shrink-0 items-center">
        {item.status === "failed" && (
          <button
            type="button"
            aria-label={t(($) => $.detail.retry_item)}
            disabled={retryItem.isPending}
            onClick={handleRetry}
            className="flex size-6 items-center justify-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:bg-accent hover:text-foreground group-hover:opacity-100 disabled:cursor-not-allowed"
          >
            <RotateCcw className="size-3.5" />
          </button>
        )}
        <button
          type="button"
          aria-label={t(($) => $.detail.delete_item)}
          disabled={!canDelete}
          onClick={handleDelete}
          className="flex size-6 items-center justify-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:bg-accent hover:text-destructive group-hover:opacity-100 disabled:cursor-not-allowed disabled:opacity-0"
        >
          <Trash2 className="size-3.5" />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add-item composer
// ---------------------------------------------------------------------------

function AddItemComposer({ queueId, existingIssueIds }: { queueId: string; existingIssueIds: string[] }) {
  const { t } = useT("queues");
  const addItems = useAddQueueItems();
  const [mode, setMode] = useState<"prompt" | "issue">("prompt");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [selectedIssues, setSelectedIssues] = useState<Issue[]>([]);
  const [agentId, setAgentId] = useState("");
  const [issuePickerOpen, setIssuePickerOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const canSubmit =
    !submitting && (mode === "prompt" ? title.trim().length > 0 : selectedIssues.length > 0);

  const reset = () => {
    setTitle("");
    setBody("");
    setSelectedIssues([]);
    setAgentId("");
  };

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      await addItems.mutateAsync({
        id: queueId,
        items:
          mode === "prompt"
            ? [
                {
                  kind: "prompt" as const,
                  title: title.trim(),
                  body: body.trim() || undefined,
                  agent_id: agentId || undefined,
                },
              ]
            : selectedIssues.map((issue) => ({
                kind: "issue" as const,
                issue_id: issue.id,
                agent_id: agentId || undefined,
              })),
      });
      toast.success(t(($) => $.detail.toast_item_added));
      reset();
    } catch (err) {
      toast.error(
        err instanceof Error && err.message ? err.message : t(($) => $.detail.toast_item_add_failed),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="border-t bg-muted/20 px-4 py-3">
      <div className="mb-2 flex items-center gap-1 rounded-md bg-muted p-1 w-fit">
        <button
          type="button"
          onClick={() => setMode("prompt")}
          className={cn(
            "rounded px-2.5 py-1 text-xs transition-colors",
            mode === "prompt" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground",
          )}
        >
          {t(($) => $.detail.composer.mode_prompt)}
        </button>
        <button
          type="button"
          onClick={() => setMode("issue")}
          className={cn(
            "rounded px-2.5 py-1 text-xs transition-colors",
            mode === "issue" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground",
          )}
        >
          {t(($) => $.detail.composer.mode_issue)}
        </button>
      </div>

      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1 space-y-1.5">
          {mode === "prompt" ? (
            <>
              <Input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={t(($) => $.detail.composer.title_placeholder)}
              />
              <Textarea
                value={body}
                onChange={(e) => setBody(e.target.value)}
                placeholder={t(($) => $.detail.composer.body_placeholder)}
                rows={2}
              />
            </>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="w-full justify-start"
              onClick={() => setIssuePickerOpen(true)}
            >
              {selectedIssues.length === 1
                ? selectedIssues[0]!.title
                : selectedIssues.length > 1
                  ? t(($) => $.detail.composer.picked_count, { count: selectedIssues.length })
                  : t(($) => $.detail.composer.pick_issue_button)}
            </Button>
          )}
        </div>

        <AgentPicker
          assignee={agentId ? { type: "agent", id: agentId } : null}
          onChange={(next) => {
            if (next.type === "agent") setAgentId(next.id);
          }}
          align="end"
        />

        <Button type="button" size="sm" disabled={!canSubmit} onClick={handleSubmit}>
          {submitting ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Plus className="size-3.5" />
          )}
          {t(($) => $.detail.composer.add_button)}
        </Button>
      </div>

      <IssuePickerModal
        open={issuePickerOpen}
        onOpenChange={setIssuePickerOpen}
        title={t(($) => $.detail.composer.pick_issue_button)}
        description={t(($) => $.detail.composer.mode_issue)}
        excludeIds={existingIssueIds}
        preloadAll
        multiple
        onSelect={(issue) => setSelectedIssues([issue])}
        onSelectMany={setSelectedIssues}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Schedule + verbs
// ---------------------------------------------------------------------------

function StartPopover({ queueId }: { queueId: string }) {
  const { t } = useT("queues");
  const startQueue = useStartQueue();
  const [open, setOpen] = useState(false);
  const [scheduledAt, setScheduledAt] = useState("");

  const handleStart = async (startAt?: string) => {
    try {
      await startQueue.mutateAsync({ id: queueId, startAt });
      toast.success(t(($) => $.detail.toast_started));
      setOpen(false);
    } catch (err) {
      toast.error(
        err instanceof Error && err.message ? err.message : t(($) => $.detail.toast_start_failed),
      );
    }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button size="sm" disabled={startQueue.isPending}>
            <Play className="size-3.5" />
            {t(($) => $.detail.start_button)}
          </Button>
        }
      />
      <PopoverContent align="end" className="w-64 space-y-2 p-3">
        <Button
          type="button"
          size="sm"
          className="w-full"
          disabled={startQueue.isPending}
          onClick={() => handleStart(undefined)}
        >
          {t(($) => $.detail.start_now)}
        </Button>
        <div className="flex items-center gap-1.5">
          <Input
            type="datetime-local"
            value={scheduledAt}
            onChange={(e) => setScheduledAt(e.target.value)}
          />
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!scheduledAt || startQueue.isPending}
            onClick={() => handleStart(new Date(scheduledAt).toISOString())}
          >
            {t(($) => $.detail.start_at_specific)}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function QueueDetailPage({ queueId }: { queueId: string }) {
  const { t } = useT("queues");
  const wsId = useWorkspaceId();
  const wsPaths = useWorkspacePaths();
  const { data, isLoading } = useQuery(queueDetailOptions(wsId, queueId));

  const pauseQueue = usePauseQueue();
  const resumeQueue = useResumeQueue();
  const clearFinished = useClearFinishedQueueItems();
  const reorderItems = useReorderQueueItems();
  const navigation = useNavigation();

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [itemIds, setItemIds] = useState<string[]>([]);
  useEffect(() => {
    if (data) {
      setItemIds([...data.items].sort((a, b) => a.seq - b.seq).map((i) => i.id));
    }
  }, [data]);

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }));

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const oldIndex = itemIds.indexOf(String(active.id));
    const newIndex = itemIds.indexOf(String(over.id));
    if (oldIndex === -1 || newIndex === -1) return;
    const next = arrayMove(itemIds, oldIndex, newIndex);
    setItemIds(next);
    reorderItems.mutate(
      { id: queueId, order: next },
      {
        onError: (err) =>
          toast.error(
            err instanceof Error && err.message ? err.message : t(($) => $.detail.toast_reorder_failed),
          ),
      },
    );
  };

  if (isLoading) {
    return (
      <div className="flex h-full flex-col">
        <div className="flex h-12 shrink-0 items-center gap-2 border-b px-5">
          <Skeleton className="h-4 w-4" />
          <span className="text-muted-foreground">/</span>
          <Skeleton className="h-4 w-32" />
        </div>
        <div className="flex-1 space-y-3 p-6">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        {t(($) => $.detail.not_found)}
      </div>
    );
  }

  const { queue, items } = data;
  const itemsById = new Map(items.map((i) => [i.id, i]));
  const existingIssueIds = items.filter((i) => i.issue_id).map((i) => i.issue_id as string);
  const hasFinishedItems = items.some((i) => i.status === "completed" || i.status === "failed");

  const handlePause = async () => {
    try {
      await pauseQueue.mutateAsync(queueId);
      toast.success(t(($) => $.detail.toast_paused));
    } catch (err) {
      toast.error(err instanceof Error && err.message ? err.message : t(($) => $.detail.toast_pause_failed));
    }
  };

  const handleResume = async () => {
    try {
      await resumeQueue.mutateAsync(queueId);
      toast.success(t(($) => $.detail.toast_resumed));
    } catch (err) {
      toast.error(err instanceof Error && err.message ? err.message : t(($) => $.detail.toast_resume_failed));
    }
  };

  const handleClearFinished = async () => {
    try {
      await clearFinished.mutateAsync(queueId);
      toast.success(t(($) => $.detail.toast_cleared));
    } catch (err) {
      toast.error(err instanceof Error && err.message ? err.message : t(($) => $.detail.toast_clear_failed));
    }
  };

  return (
    <div className="flex h-full flex-col">
      <BreadcrumbHeader
        segments={[{ href: wsPaths.queues(), label: t(($) => $.page.title) }]}
        leaf={
          <>
            <h1 className="min-w-0 truncate text-sm font-medium text-foreground">{queue.name}</h1>
            <span className="ml-1 shrink-0">
              <QueueStatusBadge status={queue.status} />
            </span>
          </>
        }
        actions={
          <>
            {queue.status === "idle" && <StartPopover queueId={queueId} />}
            {(queue.status === "running" || queue.status === "scheduled") && (
              <Button size="sm" variant="outline" disabled={pauseQueue.isPending} onClick={handlePause}>
                <Pause className="size-3.5" />
                {t(($) => $.detail.pause_button)}
              </Button>
            )}
            {queue.status === "paused" && (
              <Button size="sm" disabled={resumeQueue.isPending} onClick={handleResume}>
                <Play className="size-3.5" />
                {t(($) => $.detail.resume_button)}
              </Button>
            )}
            <Button
              size="sm"
              variant="outline"
              disabled={!hasFinishedItems || clearFinished.isPending}
              onClick={handleClearFinished}
            >
              <RotateCw className="size-3.5" />
              {t(($) => $.detail.clear_finished_button)}
            </Button>
            <Button
              size="sm"
              variant="outline"
              aria-label={t(($) => $.detail.delete_queue_button)}
              className="text-muted-foreground hover:text-destructive"
              onClick={() => setDeleteOpen(true)}
            >
              <Trash2 className="size-3.5" />
            </Button>
          </>
        }
      />

      {/* Schedule summary */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1.5 border-b bg-muted/20 px-5 py-2.5 text-xs text-muted-foreground">
        <span>
          {t(($) => $.detail.start_at_label)}:{" "}
          <span className="text-foreground">
            {queue.start_at ? new Date(queue.start_at).toLocaleString() : "—"}
          </span>
        </span>
        <span>
          {t(($) => $.detail.delay_label)}:{" "}
          <span className="text-foreground">
            {t(($) => $.detail.delay_minutes, { count: Math.round(queue.item_delay_seconds / 60) })}
          </span>
        </span>
        <span>
          {t(($) => $.detail.cron_label)}:{" "}
          <span className="text-foreground">{queue.cron_expression ?? "—"}</span>
        </span>
        <span>
          {t(($) => $.detail.next_run_label)}:{" "}
          <span className="text-foreground">
            {queue.next_run_at ? new Date(queue.next_run_at).toLocaleString() : "—"}
          </span>
        </span>
      </div>

      <div className="flex-1 overflow-y-auto">
        {items.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-16 text-center text-muted-foreground">
            <ListChecks className="size-8 opacity-50" />
            <p className="text-sm">{t(($) => $.detail.items_empty)}</p>
          </div>
        ) : (
          <DndContext sensors={sensors} onDragEnd={handleDragEnd}>
            <SortableContext items={itemIds} strategy={verticalListSortingStrategy}>
              {itemIds.map((id) => {
                const item = itemsById.get(id);
                if (!item) return null;
                return <ItemRow key={id} item={item} queueId={queueId} />;
              })}
            </SortableContext>
          </DndContext>
        )}
      </div>

      <AddItemComposer queueId={queueId} existingIssueIds={existingIssueIds} />

      <DeleteQueueDialog
        queue={queue}
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        onDeleted={() => navigation.push(wsPaths.queues())}
      />
    </div>
  );
}
