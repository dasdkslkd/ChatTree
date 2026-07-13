import { apiClient } from './client';

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

type TaskNotificationCacheEntry = {
  etag: string;
  data: TaskNotificationRecord[];
};

const taskNotificationCache = new Map<string, TaskNotificationCacheEntry>();

export const taskNotificationsApi = {
  list: async (conversationId: string): Promise<TaskNotificationRecord[]> => {
    const cached = taskNotificationCache.get(conversationId);
    const response = await apiClient.get(`/conversations/${encodeURIComponent(conversationId)}/task-notifications`, {
      headers: cached?.etag ? { 'If-None-Match': cached.etag } : undefined,
      validateStatus: (status) => (status >= 200 && status < 300) || status === 304,
    });
    if (response.status === 304) return cached?.data ?? [];
    const data = Array.isArray(response.data) ? response.data : [];
    const etag = response.headers?.etag;
    if (typeof etag === 'string' && etag) {
      taskNotificationCache.set(conversationId, { etag, data });
    } else {
      taskNotificationCache.delete(conversationId);
    }
    return data;
  },

  bind: async (
    notificationId: string,
    deliveryNodeId: string,
    options: { trigger?: boolean } = {},
  ): Promise<TaskNotificationRecord> => {
    const response = await apiClient.post(`/task-notifications/${encodeURIComponent(notificationId)}/bind`, {
      delivery_node_id: deliveryNodeId,
      trigger: options.trigger ?? true,
    });
    return response.data;
  },

  delete: async (notificationId: string): Promise<TaskNotificationRecord> => {
    const response = await apiClient.post(`/task-notifications/${encodeURIComponent(notificationId)}/delete`, {});
    return response.data;
  },
};
