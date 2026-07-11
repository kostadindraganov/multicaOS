import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { QueueDetailPage as QueueDetail } from "@multica/views/queues/components";
import { useWorkspaceId } from "@multica/core/hooks";
import { queueDetailOptions } from "@multica/core/queues/queries";
import { useDocumentTitle } from "@/hooks/use-document-title";

export function QueueDetailPage() {
  const { id } = useParams<{ id: string }>();
  const wsId = useWorkspaceId();
  const { data } = useQuery(queueDetailOptions(wsId, id!));

  useDocumentTitle(data?.queue ? data.queue.name : "Queues");

  if (!id) return null;
  return <QueueDetail queueId={id} />;
}
