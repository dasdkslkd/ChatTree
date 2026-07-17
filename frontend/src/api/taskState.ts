import { apiClient } from './client';
import type { ActiveTaskRecord } from '../types/task';
import {
  captureConnectionEpoch,
  connectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';

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

function resolveEpochToken(token?: ConnectionEpochToken): ConnectionEpochToken {
  if (token) {
    connectionEpochRuntime.assertCurrent(token);
    return token;
  }
  return captureConnectionEpoch();
}

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

function readCachedTaskState(
  conversationId: string,
  token: ConnectionEpochToken,
): TaskStateCacheEntry | undefined {
  connectionEpochRuntime.assertCurrent(token);
  const cached = taskStateCache.get(conversationId);
  connectionEpochRuntime.assertCurrent(token);
  return cached;
}

function responseEtag(
  headers: unknown,
  token: ConnectionEpochToken,
): string | undefined {
  connectionEpochRuntime.assertCurrent(token);
  const candidate = headers && typeof headers === 'object'
    ? (headers as { etag?: unknown }).etag
    : undefined;
  connectionEpochRuntime.assertCurrent(token);
  return typeof candidate === 'string' ? candidate : undefined;
}

export function storeTaskState(
  conversationId: string,
  state: TaskStateSnapshot,
  etag?: string,
  ownerToken?: ConnectionEpochToken,
): TaskStateSnapshot {
  const token = resolveEpochToken(ownerToken);
  connectionEpochRuntime.assertCurrent(token);
  const cacheEtag = etag || (state.version ? `"${state.version}"` : '');
  if (cacheEtag) {
    taskStateCache.set(conversationId, { etag: cacheEtag, data: state });
  } else {
    taskStateCache.delete(conversationId);
  }
  return state;
}

export const taskStateApi = {
  fetch: async (conversationId: string, ownerToken?: ConnectionEpochToken): Promise<TaskStateSnapshot> => {
    const token = resolveEpochToken(ownerToken);
    const cached = readCachedTaskState(conversationId, token);
    const response = await apiClient.get(`/conversations/${encodeURIComponent(conversationId)}/task-state`, {
      headers: cached?.etag ? { 'If-None-Match': cached.etag } : undefined,
      validateStatus: (status) => (status >= 200 && status < 300) || status === 304,
    });
    connectionEpochRuntime.assertCurrent(token);
    const status = response.status;
    connectionEpochRuntime.assertCurrent(token);
    if (status === 304) {
      const latestCached = readCachedTaskState(conversationId, token);
      if (!latestCached) {
        throw new Error('Task state cache changed before a 304 response completed');
      }
      connectionEpochRuntime.assertCurrent(token);
      return latestCached.data;
    }
    const responseData = response.data;
    connectionEpochRuntime.assertCurrent(token);
    const state = normalizeTaskState(responseData, conversationId);
    connectionEpochRuntime.assertCurrent(token);
    const etag = responseEtag(response.headers, token);
    return storeTaskState(conversationId, state, etag, token);
  },

  bind: async (
    conversationId: string,
    notificationId: string,
    deliveryNodeId: string,
    options: { trigger?: boolean } = {},
    ownerToken?: ConnectionEpochToken,
  ): Promise<TaskStateSnapshot> => {
    const token = resolveEpochToken(ownerToken);
    const response = await apiClient.post(`/task-notifications/${encodeURIComponent(notificationId)}/bind`, {
      delivery_node_id: deliveryNodeId,
      trigger: options.trigger ?? true,
    });
    connectionEpochRuntime.assertCurrent(token);
    const responseData = response.data;
    connectionEpochRuntime.assertCurrent(token);
    const state = normalizeTaskState(responseData, conversationId);
    connectionEpochRuntime.assertCurrent(token);
    const etag = responseEtag(response.headers, token);
    return storeTaskState(state.conversation_id, state, etag, token);
  },

  delete: async (
    conversationId: string,
    notificationId: string,
    ownerToken?: ConnectionEpochToken,
  ): Promise<TaskStateSnapshot> => {
    const token = resolveEpochToken(ownerToken);
    const response = await apiClient.post(`/task-notifications/${encodeURIComponent(notificationId)}/delete`, {});
    connectionEpochRuntime.assertCurrent(token);
    const responseData = response.data;
    connectionEpochRuntime.assertCurrent(token);
    const state = normalizeTaskState(responseData, conversationId);
    connectionEpochRuntime.assertCurrent(token);
    const etag = responseEtag(response.headers, token);
    return storeTaskState(state.conversation_id, state, etag, token);
  },

  clear: (conversationId: string): void => {
    taskStateCache.delete(conversationId);
  },
};
