import { apiClient } from '../api/client';
import type { CreateTaskRequest, TaskRecord, UpdateTaskRequest } from '../types/task';

export const taskLedgerService = {
  fetchConversationTasks: async (
    conversationId: string,
    includeFinished = false,
  ): Promise<TaskRecord[]> => {
    const response = await apiClient.get(`/conversations/${conversationId}/tasks`, {
      params: { include_finished: includeFinished },
    });
    return response.data;
  },

  createConversationTask: async (
    conversationId: string,
    request: CreateTaskRequest,
  ): Promise<TaskRecord> => {
    const response = await apiClient.post(`/conversations/${conversationId}/tasks`, request);
    return response.data;
  },

  updateConversationTask: async (
    conversationId: string,
    taskId: string,
    request: UpdateTaskRequest,
  ): Promise<TaskRecord> => {
    const response = await apiClient.patch(`/conversations/${conversationId}/tasks/${taskId}`, request);
    return response.data;
  },
};
