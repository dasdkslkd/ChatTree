import { apiClient } from './client';
import type { RunStartResponse } from './runs';

export interface StartWorkflowRequest {
  script: string;
  args?: Record<string, unknown>;
  parent_node_id?: string | null;
  created_by_run_id?: string | null;
  cancellation_parent_run_id?: string | null;
  budget?: Record<string, unknown>;
}

export const workflowsApi = {
  validate: async (script: string): Promise<{ valid: boolean }> => {
    const response = await apiClient.post('/workflows/validate', { script });
    return response.data;
  },

  startRun: async (
    conversationId: string,
    request: StartWorkflowRequest,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<RunStartResponse> => {
    const response = await apiClient.post<RunStartResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/workflows/runs`,
      request,
      {
        headers: { 'Idempotency-Key': idempotencyKey },
        signal,
      },
    );
    return response.data;
  },

  graph: async (runId: string): Promise<Record<string, unknown>> => {
    const response = await apiClient.get(`/workflows/${runId}/graph`);
    return response.data;
  },
};
