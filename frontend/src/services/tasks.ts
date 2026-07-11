import { apiClient } from '../api/client';
import type { ActiveTaskRecord } from '../types/task';

export const activeTaskService = {
  fetch: async (conversationId: string): Promise<ActiveTaskRecord | null> => {
    const response = await apiClient.get(`/conversations/${conversationId}/task`);
    return response.data;
  },
};
