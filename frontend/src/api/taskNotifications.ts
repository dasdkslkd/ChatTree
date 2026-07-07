import { apiClient } from './client';

export interface TaskNotificationRecord {
  id: string;
  conversation_id: string;
  source_run_id: string;
  source_run_kind: string;
  task_id?: string | null;
  status: 'unbound' | 'bound' | 'delivering' | 'delivered' | 'observed' | 'deleted';
  delivery_node_id?: string | null;
  delivered_run_id?: string | null;
  delivered_node_id?: string | null;
  summary: string;
  content: string;
  payload?: Record<string, unknown>;
  created_at: number;
  updated_at: number;
}

export const taskNotificationsApi = {
  list: async (conversationId: string): Promise<TaskNotificationRecord[]> => {
    const response = await apiClient.get(`/conversations/${encodeURIComponent(conversationId)}/task-notifications`);
    return response.data;
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
