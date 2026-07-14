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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@multica/ui/components/ui/select";
import { Switch } from "@multica/ui/components/ui/switch";
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
import { ProjectPicker } from "../../projects/components/project-picker";
import { useT } from "../../i18n";
import {
  buildCron,
  parseCron,
  DEFAULT_TIMEZONE,
  TIMEZONE_OPTIONS,
  type ScheduleFrequency,
} from "./cron-schedule";

// Cron day-of-week values in Monday-first display order, and their i18n keys
// indexed by cron value (Sunday = 0).
const WEEKDAY_ORDER = [1, 2, 3, 4, 5, 6, 0] as const;
const WEEKDAY_KEYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"] as const;

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
  const [projectId, setProjectId] = useState<string | null>(initial?.project_id ?? null);
  // Delay is stored server-side in seconds; the form works in minutes for
  // usability, converting ×60 on submit.
  const [delayMinutes, setDelayMinutes] = useState(
    initial ? Math.round(initial.item_delay_seconds / 60) : 0,
  );
  // Schedule is edited through dropdowns (frequency / time / weekday) and
  // converted to a cron expression on submit. An existing expression the
  // dropdowns cannot represent falls back to a raw "custom" input so editing
  // never mangles a hand-written cron.
  const parsedCron = parseCron(initial?.cron_expression ?? "");
  const [frequency, setFrequency] = useState<ScheduleFrequency>(
    parsedCron ? parsedCron.frequency : "custom",
  );
  const [hour, setHour] = useState(parsedCron?.hour ?? 9);
  const [dayOfWeek, setDayOfWeek] = useState(parsedCron?.dayOfWeek ?? 1);
  const [customCron, setCustomCron] = useState(initial?.cron_expression ?? "");
  const [runOnce, setRunOnce] = useState(initial?.run_once === true);
  const [timezone, setTimezone] = useState(initial?.timezone || DEFAULT_TIMEZONE);
  const [submitting, setSubmitting] = useState(false);

  // An edited queue may carry a timezone outside the curated list; keep it
  // selectable so editing doesn't silently rewrite it.
  const timezoneChoices: string[] = TIMEZONE_OPTIONS.includes(
    timezone as (typeof TIMEZONE_OPTIONS)[number],
  )
    ? [...TIMEZONE_OPTIONS]
    : [timezone, ...TIMEZONE_OPTIONS];

  const createQueue = useCreateQueue();
  const updateQueue = useUpdateQueue();

  const canSubmit = name.trim().length > 0 && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const cronExpression =
        frequency === "custom" ? customCron.trim() : buildCron({ frequency, hour, dayOfWeek });
      const data = {
        name: name.trim(),
        description: description.trim() || undefined,
        default_agent_id: agentId || undefined,
        project_id: projectId,
        item_delay_seconds: Math.max(0, delayMinutes) * 60,
        cron_expression: cronExpression || undefined,
        timezone: cronExpression ? timezone : undefined,
        run_once: runOnce,
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
              {t(($) => $.dialog.project_label)}
            </label>
            <ProjectPicker
              projectId={projectId}
              onUpdate={(updates) => setProjectId(updates.project_id ?? null)}
              align="start"
              triggerRender={
                <button
                  type="button"
                  className="flex w-full items-center gap-2 rounded-md border bg-background px-2.5 py-1.5 text-left text-sm hover:bg-accent/40 transition-colors cursor-pointer"
                />
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
                {t(($) => $.dialog.schedule_label)}
              </label>
              <Select
                value={frequency}
                onValueChange={(v) => v && setFrequency(v as ScheduleFrequency)}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">{t(($) => $.dialog.schedule_none)}</SelectItem>
                  <SelectItem value="hourly">{t(($) => $.dialog.schedule_hourly)}</SelectItem>
                  <SelectItem value="daily">{t(($) => $.dialog.schedule_daily)}</SelectItem>
                  <SelectItem value="weekly">{t(($) => $.dialog.schedule_weekly)}</SelectItem>
                  <SelectItem value="custom">{t(($) => $.dialog.schedule_custom)}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {frequency === "weekly" && (
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">
                  {t(($) => $.dialog.schedule_day_label)}
                </label>
                <Select
                  value={String(dayOfWeek)}
                  onValueChange={(v) => v && setDayOfWeek(Number(v))}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {WEEKDAY_ORDER.map((d) => (
                      <SelectItem key={d} value={String(d)}>
                        {t(($) => $.dialog.weekdays[WEEKDAY_KEYS[d]])}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            {(frequency === "daily" || frequency === "weekly") && (
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">
                  {t(($) => $.dialog.schedule_time_label)}
                </label>
                <Select value={String(hour)} onValueChange={(v) => v && setHour(Number(v))}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {Array.from({ length: 24 }, (_, h) => (
                      <SelectItem key={h} value={String(h)}>
                        {String(h).padStart(2, "0")}:00
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            {frequency === "custom" && (
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">
                  {t(($) => $.dialog.cron_label)}
                </label>
                <Input
                  value={customCron}
                  onChange={(e) => setCustomCron(e.target.value)}
                  placeholder={t(($) => $.dialog.cron_placeholder)}
                />
              </div>
            )}
            {frequency !== "none" && (
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-muted-foreground">
                  {t(($) => $.dialog.timezone_label)}
                </label>
                <Select value={timezone} onValueChange={(v) => v && setTimezone(v)}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {timezoneChoices.map((tz) => (
                      <SelectItem key={tz} value={tz}>
                        {tz}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>

          {frequency !== "none" && (
            <div className="flex items-center justify-between rounded-md border px-3 py-2">
              <div className="space-y-0.5">
                <label
                  htmlFor="queue-run-once"
                  className="text-xs font-medium cursor-pointer"
                >
                  {t(($) => $.dialog.run_once_label)}
                </label>
                <p className="text-xs text-muted-foreground">
                  {t(($) => $.dialog.run_once_hint)}
                </p>
              </div>
              <Switch id="queue-run-once" checked={runOnce} onCheckedChange={setRunOnce} />
            </div>
          )}
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
