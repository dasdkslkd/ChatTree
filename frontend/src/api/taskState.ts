import { apiClient } from './client';
import type { ActiveTaskRecord } from '../types/task';

export interface TaskNotificationRecord {
  id: string;
  conversation_id: string;
  source_run_id: string;
  source_run_kind: string;
  status: 'unbound' | 'bound' | 'delivering' | 'delivery_failed' | 'delivery_cancelled' | 'delivered' | 'observed' | 'deleted';
  delivery_node_id?: string | null;
  delivered_run_id?: string | null;
  delivered_node_id?: string | null;
  summary: string;
  content: string;
  payload?: Record<string, unknown>;
  created_at: number;
  updated_at: number;
}

export interface TaskStateFlags {
  running: boolean;
  delivering: boolean;
  needsFollowup: boolean;
}

export interface TaskStateSnapshot {
  conversation_id: string;
  task: ActiveTaskRecord | null;
  notifications: TaskNotificationRecord[];
  flags: TaskStateFlags;
  version: string;
}

type TaskStateCacheEntry = {
  etag: string;
  data: TaskStateSnapshot;
};

const taskStateCache = new Map<string, TaskStateCacheEntry>();

function normalizeTaskState(data: unknown, conversationId: string): TaskStateSnapshot {
  const candidate = data && typeof data === 'object' ? data as Partial<TaskStateSnapshot> : {};
  return {
    conversation_id: typeof candidate.conversation_id === 'string' && candidate.conversation_id
      ? candidate.conversation_id
      : conversationId,
    task: candidate.task ?? null,
    notifications: Array.isArray(candidate.notifications) ? candidate.notifications : [],
    flags: {
      running: Boolean(candidate.flags?.running),
      delivering: Boolean(candidate.flags?.delivering),
      needsFollowup: Boolean(candidate.flags?.needsFollowup),
    },
    version: typeof candidate.version === 'string' ? candidate.version : '',
  };
}

export function storeTaskState(conversationId: string, state: TaskStateSnapshot, etag?: string): TaskStateSnapshot {
  const cacheEtag = etag || (state.version ? `"${state.version}"` : '');
  if (cacheEtag) {
    taskStateCache.set(conversationId, { etag: cacheEtag, data: state });
  } else {
    taskStateCache.delete(conversationId);
  }
  return state;
}

export const taskStateApi = {
  fetch: async (conversationId: string): Promise<TaskStateSnapshot> => {
    const cached = taskStateCache.get(conversationId);
    const response = await apiClient.get(`/conversations/${encodeURIComponent(conversationId)}/task-state`, {
      headers: cached?.etag ? { 'If-None-Match': cached.etag } : undefined,
      validateStatus: (status) => (status >= 200 && status < 300) || status === 304,
    });
    if (response.status === 304 && cached) return cached.data;
    const state = normalizeTaskState(response.data, conversationId);
    const etag = response.headers?.etag;
    return storeTaskState(conversationId, state, typeof etag === 'string' ? etag : undefined);
  },

  bind: async (
    conversationId: string,
    notificationId: string,
    deliveryNodeId: string,
    options: { trigger?: boolean } = {},
  ): Promise<TaskStateSnapshot> => {
    const response = await apiClient.post(`/task-notifications/${encodeURIComponent(notificationId)}/bind`, {
      delivery_node_id: deliveryNodeId,
      trigger: options.trigger ?? true,
    });
    const state = normalizeTaskState(response.data, conversationId);
    const etag = response.headers?.etag;
    return storeTaskState(state.conversation_id, state, typeof etag === 'string' ? etag : undefined);
  },

  delete: async (conversationId: string, notificationId: string): Promise<TaskStateSnapshot> => {
    const response = await apiClient.post(`/task-notifications/${encodeURIComponent(notificationId)}/delete`, {});
    const state = normalizeTaskState(response.data, conversationId);
    const etag = response.headers?.etag;
    return storeTaskState(state.conversation_id, state, typeof etag === 'string' ? etag : undefined);
  },

  clear: (conversationId: string): void => {
    taskStateCache.delete(conversationId);
  },
};
