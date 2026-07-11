"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Bot } from "lucide-react";
import { useCreateQueue, useUpdateQueue } from "@multica/core/queues";
import { useWorkspaceId } from "@multica/core/hooks";
import { agentListOptions } from "@multica/core/workspace/queries";
import type { WorkQueue } from "@multica/core/types";
import { Button } from "@multica/ui/components/ui/button";
import { Input } from "@multica/ui/components/ui/input";
import { Textarea } from "@multica/ui/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@multica/ui/components/ui/dialog";
import { ActorAvatar } from "../../common/actor-avatar";
import { AgentPicker } from "../../autopilots/components/pickers/agent-picker";
import { useT } from "../../i18n";

export type QueueDialogProps =
  | { mode: "create"; open: boolean; onOpenChange: (v: boolean) => void }
  | {
      mode: "edit";
      open: boolean;
      onOpenChange: (v: boolean) => void;
      queue: WorkQueue;
    };

export function QueueDialog(props: QueueDialogProps) {
  const { t } = useT("queues");
  const { mode, open, onOpenChange } = props;
  const isCreate = mode === "create";
  const initial = isCreate ? undefined : props.queue;
  const wsId = useWorkspaceId();
  const { data: agents = [] } = useQuery(agentListOptions(wsId));

  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [agentId, setAgentId] = useState<string>(initial?.default_agent_id ?? "");
  // Delay is stored server-side in seconds; the form works in minutes for
  // usability, converting ×60 on submit.
  const [delayMinutes, setDelayMinutes] = useState(
    initial ? Math.round(initial.item_delay_seconds / 60) : 0,
  );
  const [cronExpression, setCronExpression] = useState(initial?.cron_expression ?? "");
  const [timezone, setTimezone] = useState(initial?.timezone ?? "");
  const [submitting, setSubmitting] = useState(false);

  const createQueue = useCreateQueue();
  const updateQueue = useUpdateQueue();

  const canSubmit = name.trim().length > 0 && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const data = {
        name: name.trim(),
        description: description.trim() || undefined,
        default_agent_id: agentId || undefined,
        item_delay_seconds: Math.max(0, delayMinutes) * 60,
        cron_expression: cronExpression.trim() || undefined,
        timezone: timezone.trim() || undefined,
      };
      if (isCreate) {
        await createQueue.mutateAsync(data);
        toast.success(t(($) => $.dialog.toast_created));
      } else {
        await updateQueue.mutateAsync({ id: props.queue.id, ...data });
        toast.success(t(($) => $.dialog.toast_updated));
      }
      onOpenChange(false);
    } catch (err) {
      toast.error(
        err instanceof Error && err.message
          ? err.message
          : isCreate
            ? t(($) => $.dialog.toast_create_failed)
            : t(($) => $.dialog.toast_update_failed),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {isCreate ? t(($) => $.dialog.create_title) : t(($) => $.dialog.edit_title)}
          </DialogTitle>
          <DialogDescription>{t(($) => $.dialog.description_hint)}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">
              {t(($) => $.dialog.name_label)}
            </label>
            <Input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t(($) => $.dialog.name_placeholder)}
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">
              {t(($) => $.dialog.description_label)}
            </label>
            <Textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t(($) => $.dialog.description_placeholder)}
              rows={2}
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">
              {t(($) => $.dialog.agent_label)}
            </label>
            <AgentPicker
              assignee={agentId ? { type: "agent", id: agentId } : null}
              onChange={(next) => {
                if (next.type === "agent") setAgentId(next.id);
              }}
              align="start"
              triggerRender={
                <button
                  type="button"
                  className="flex w-full items-center gap-2 rounded-md border bg-background px-2.5 py-1.5 text-left text-sm hover:bg-accent/40 transition-colors cursor-pointer"
                >
                  {agentId ? (
                    <ActorAvatar actorType="agent" actorId={agentId} size="sm" />
                  ) : (
                    <Bot className="size-3.5 text-muted-foreground" />
                  )}
                  <span className="min-w-0 flex-1 truncate">
                    {agentId
                      ? agents.find((a) => a.id === agentId)?.name ?? agentId
                      : t(($) => $.dialog.select_agent)}
                  </span>
                </button>
              }
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">
              {t(($) => $.dialog.delay_label)}
            </label>
            <Input
              type="number"
              min={0}
              value={delayMinutes}
              onChange={(e) => setDelayMinutes(Number(e.target.value) || 0)}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">
                {t(($) => $.dialog.cron_label)}
              </label>
              <Input
                value={cronExpression}
                onChange={(e) => setCronExpression(e.target.value)}
                placeholder={t(($) => $.dialog.cron_placeholder)}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">
                {t(($) => $.dialog.timezone_label)}
              </label>
              <Input
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                placeholder={t(($) => $.dialog.timezone_placeholder)}
              />
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={submitting}
            onClick={() => onOpenChange(false)}
          >
            {t(($) => $.dialog.cancel)}
          </Button>
          <Button type="button" size="sm" disabled={!canSubmit} onClick={handleSubmit}>
            {submitting
              ? isCreate
                ? t(($) => $.dialog.creating)
                : t(($) => $.dialog.saving)
              : isCreate
                ? t(($) => $.dialog.create)
                : t(($) => $.dialog.save)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
