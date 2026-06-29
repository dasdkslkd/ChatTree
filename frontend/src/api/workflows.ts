import { apiClient } from './client';
import type { RunRecord } from '../types/run';

export interface StartWorkflowRequest {
  script: string;
  args?: Record<string, unknown>;
  parent_node_id?: string | null;
  parent_run_id?: string | null;
  budget?: Record<string, unknown>;
}

export const workflowsApi = {
  validate: async (script: string): Promise<{ valid: boolean }> => {
    const response = await apiClient.post('/workflows/validate', { script });
    return response.data;
  },

  startRun: async (conversationId: string, request: StartWorkflowRequest): Promise<RunRecord> => {
    const response = await apiClient.post(`/conversations/${conversationId}/workflows/runs`, request);
    return response.data;
  },

  graph: async (runId: string): Promise<Record<string, unknown>> => {
    const response = await apiClient.get(`/workflows/${runId}/graph`);
    return response.data;
  },
};
