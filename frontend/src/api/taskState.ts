import { apiClient } from './client';
import type { ActiveTaskRecord } from '../types/task';

export interface TaskStateFlags {
  running: boolean;
}

export interface TaskStateSnapshot {
  conversation_id: string;
  task: ActiveTaskRecord | null;
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
    flags: {
      running: Boolean(candidate.flags?.running),
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

  clear: (conversationId: string): void => {
    taskStateCache.delete(conversationId);
  },
};
