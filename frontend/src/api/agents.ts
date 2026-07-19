import { apiClient } from './client';
import type { RunStartResponse } from './runs';

export interface StartSubagentRequest {
  input: unknown;
  parent_node_id?: string | null;
  created_by_run_id?: string | null;
  cancellation_parent_run_id?: string | null;
  provider_id?: string | null;
  model_id?: string | null;
  permission_mode?: string | null;
  workspace?: Record<string, unknown> | null;
}

export const agentsApi = {
  get: async (agentName: string): Promise<Record<string, unknown>> => {
    const response = await apiClient.get(`/agents/${encodeURIComponent(agentName)}`);
    return response.data;
  },

  startRun: async (
    conversationId: string,
    agentName: string,
    request: StartSubagentRequest,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<RunStartResponse> => {
    const response = await apiClient.post<RunStartResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/agents/${encodeURIComponent(agentName)}/runs`,
      request,
      {
        headers: { 'Idempotency-Key': idempotencyKey },
        signal,
      },
    );
    return response.data;
  },
};
