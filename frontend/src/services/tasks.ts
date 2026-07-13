import { apiClient } from '../api/client';
import type { ActiveTaskRecord } from '../types/task';

type ActiveTaskCacheEntry = {
  etag: string;
  data: ActiveTaskRecord | null;
};

const activeTaskCache = new Map<string, ActiveTaskCacheEntry>();

export const activeTaskService = {
  fetch: async (conversationId: string): Promise<ActiveTaskRecord | null> => {
    const cached = activeTaskCache.get(conversationId);
    const response = await apiClient.get(`/conversations/${conversationId}/task`, {
      headers: cached?.etag ? { 'If-None-Match': cached.etag } : undefined,
      validateStatus: (status) => (status >= 200 && status < 300) || status === 304,
    });
    if (response.status === 304) return cached?.data ?? null;
    const etag = response.headers?.etag;
    if (typeof etag === 'string' && etag) {
      activeTaskCache.set(conversationId, { etag, data: response.data ?? null });
    } else {
      activeTaskCache.delete(conversationId);
    }
    return response.data ?? null;
  },
};
