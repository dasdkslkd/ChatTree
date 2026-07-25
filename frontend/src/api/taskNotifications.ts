import { apiClient } from './client';
import type { TranscriptPatch } from '../types/transcript';

export type TaskNotificationRecord = Record<string, unknown>;

export interface TaskNotificationListResponse {
  conversation_id: string;
  notifications: TaskNotificationRecord[];
}

export interface TaskNotificationMutationResponse {
  notification: TaskNotificationRecord;
  transcript_patch: TranscriptPatch | null;
}

function normalizeTranscriptPatch(value: unknown): TranscriptPatch | null {
  const patch = value && typeof value === 'object' ? value as Partial<TranscriptPatch> : null;
  if (
    patch?.type !== 'transcript_patch'
    || typeof patch.conversation_id !== 'string'
    || typeof patch.node_id !== 'string'
    || typeof patch.revision !== 'number'
    || !Array.isArray(patch.operations)
  ) {
    return null;
  }
  return patch as TranscriptPatch;
}

function normalizeMutationResponse(data: unknown): TaskNotificationMutationResponse {
  const response = data && typeof data === 'object'
    ? data as { notification?: TaskNotificationRecord; transcript_patch?: unknown }
    : {};
  return {
    notification: response.notification && typeof response.notification === 'object'
      ? response.notification
      : {},
    transcript_patch: normalizeTranscriptPatch(response.transcript_patch),
  };
}

export const taskNotificationsApi = {
  list: async (conversationId: string): Promise<TaskNotificationListResponse> => {
    const response = await apiClient.get(
      `/conversations/${encodeURIComponent(conversationId)}/task-notifications`,
    );
    const data = response.data && typeof response.data === 'object'
      ? response.data as Partial<TaskNotificationListResponse>
      : {};
    return {
      conversation_id: typeof data.conversation_id === 'string' ? data.conversation_id : conversationId,
      notifications: Array.isArray(data.notifications) ? data.notifications : [],
    };
  },

  bind: async (
    conversationId: string,
    notificationId: string,
    deliveryNodeId: string,
  ): Promise<TaskNotificationMutationResponse> => {
    const response = await apiClient.post(
      `/conversations/${encodeURIComponent(conversationId)}/task-notifications/${encodeURIComponent(notificationId)}/bind`,
      { delivery_node_id: deliveryNodeId },
    );
    return normalizeMutationResponse(response.data);
  },

  delete: async (
    conversationId: string,
    notificationId: string,
  ): Promise<TaskNotificationMutationResponse> => {
    const response = await apiClient.delete(
      `/conversations/${encodeURIComponent(conversationId)}/task-notifications/${encodeURIComponent(notificationId)}`,
    );
    return normalizeMutationResponse(response.data);
  },
};
