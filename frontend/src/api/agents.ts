import { apiClient } from './client';
import type { RunRecord } from '../types/run';

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
  ): Promise<RunRecord> => {
    const response = await apiClient.post(
      `/conversations/${conversationId}/agents/${encodeURIComponent(agentName)}/runs`,
      request,
    );
    return response.data;
  },
};
