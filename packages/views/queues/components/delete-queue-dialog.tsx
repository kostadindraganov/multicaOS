"use client";

import { useState } from "react";
import { toast } from "sonner";
import { useDeleteQueue } from "@multica/core/queues";
import type { WorkQueue } from "@multica/core/types";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@multica/ui/components/ui/alert-dialog";
import { useT } from "../../i18n";

export function DeleteQueueDialog({
  queue,
  open,
  onOpenChange,
  onDeleted,
}: {
  queue: WorkQueue;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** Called only after the server confirmed the delete (never optimistic). */
  onDeleted?: () => void;
}) {
  const { t } = useT("queues");
  const deleteQueue = useDeleteQueue();
  const [deleting, setDeleting] = useState(false);

  const handleDelete = async () => {
    setDeleting(true);
    try {
      await deleteQueue.mutateAsync(queue.id);
      toast.success(t(($) => $.delete.toast_deleted));
      onOpenChange(false);
      onDeleted?.();
    } catch (err) {
      toast.error(
        err instanceof Error && err.message ? err.message : t(($) => $.delete.toast_delete_failed),
      );
    } finally {
      setDeleting(false);
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t(($) => $.delete.title, { name: queue.name })}</AlertDialogTitle>
          <AlertDialogDescription>
            {t(($) => $.delete.description)}
            {queue.status === "running" && <> {t(($) => $.delete.running_warning)}</>}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleting}>{t(($) => $.delete.cancel)}</AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            disabled={deleting}
            onClick={(e) => {
              // Keep the dialog open until the server confirms; close in
              // handleDelete's success path instead of on click.
              e.preventDefault();
              void handleDelete();
            }}
          >
            {deleting ? t(($) => $.delete.deleting) : t(($) => $.delete.confirm)}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
