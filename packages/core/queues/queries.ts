import { queryOptions } from "@tanstack/react-query";
import { api } from "../api";

export const queueKeys = {
  all: (wsId: string) => ["queues", wsId] as const,
  list: (wsId: string) => [...queueKeys.all(wsId), "list"] as const,
  detail: (wsId: string, id: string) => [...queueKeys.all(wsId), "detail", id] as const,
};

export function queueListOptions(wsId: string) {
  return queryOptions({
    queryKey: queueKeys.list(wsId),
    queryFn: () => api.listQueues(),
    select: (data) => data.queues,
  });
}

export function queueDetailOptions(wsId: string, id: string) {
  return queryOptions({
    queryKey: queueKeys.detail(wsId, id),
    queryFn: () => api.getQueue(id),
  });
}
