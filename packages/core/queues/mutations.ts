import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { queueKeys } from "./queries";
import { useWorkspaceId } from "../hooks";
import type { CreateQueueRequest, UpdateQueueRequest, AddQueueItemsRequest } from "../types";

// All queue mutations follow the verb-mutation shape (see
// useTriggerAutopilot in autopilots/mutations.ts): no optimistic updates,
// just invalidate the workspace's queue cache on settle. Queue state
// (running/paused/item progress) is server-driven and can change out from
// under the client at any time, so patching it locally isn't worth the risk.

export function useCreateQueue() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: (data: CreateQueueRequest) => api.createQueue(data),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useUpdateQueue() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string } & UpdateQueueRequest) => api.updateQueue(id, data),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useDeleteQueue() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: (id: string) => api.deleteQueue(id),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useAddQueueItems() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string } & AddQueueItemsRequest) => api.addQueueItems(id, data),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useUpdateQueueItem() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: ({
      id,
      itemId,
      ...data
    }: {
      id: string;
      itemId: string;
      title?: string;
      body?: string;
      agent_id?: string | null;
    }) => api.updateQueueItem(id, itemId, data),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useDeleteQueueItem() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: ({ id, itemId }: { id: string; itemId: string }) => api.deleteQueueItem(id, itemId),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useRetryQueueItem() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: ({ id, itemId }: { id: string; itemId: string }) => api.retryQueueItem(id, itemId),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useReorderQueueItems() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: ({ id, order }: { id: string; order: string[] }) => api.reorderQueueItems(id, order),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useStartQueue() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: ({ id, startAt }: { id: string; startAt?: string }) => api.startQueue(id, startAt),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function usePauseQueue() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: (id: string) => api.pauseQueue(id),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useResumeQueue() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: (id: string) => api.resumeQueue(id),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}

export function useClearFinishedQueueItems() {
  const qc = useQueryClient();
  const wsId = useWorkspaceId();
  return useMutation({
    mutationFn: (id: string) => api.clearFinishedQueueItems(id),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queueKeys.all(wsId) });
    },
  });
}
