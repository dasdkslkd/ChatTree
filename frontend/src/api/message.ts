import { apiClient } from './client';
import type { SendMessageRequest } from '../types/message';
import type { TranscriptPatch } from '../types/transcript';
import { perfNow, recordSpan } from '../perf/marks';
import { leaseGuardedFetch } from './leaseFetch';
import { runsApi, parseTranscriptPatchSseResponse } from './runs';
import type { RunStartResponse } from './runs';
import {
  ChatTreeApiError,
  unexpectedApiResponse,
} from './errors';

export type ToolResultSlice = {
  tool_result_id: string;
  tool_name?: string | null;
  diff_before?: Record<string, { before: string; existed: boolean; after?: string }> | null;
  offset: number;
  limit: number;
  next_offset?: number | null;
  total_chars: number;
  has_more: boolean;
  content: string;
};

export interface ActiveStreamInfo {
  run_id?: string | null;
  conversation_id: string;
  anchor_node_id?: string | null;
  node_id: string | null;
  target_node_id?: string | null;
  kind?: string;
  status?: string;
  event_count: number;
  done: boolean;
  created_at: number;
  updated_at: number;
}

export type MessageAttachStreamOptions = {
  signal?: AbortSignal;
};

export type MessageRunStartResponse = RunStartResponse;

export type PlanActionStreamOptions = {
  signal?: AbortSignal;
};

export type ToolApprovalDecisionResponse = {
  tool_call_id: string;
  status: 'approved' | 'denied' | 'expired' | 'cancelled';
  scope: 'once' | 'session' | null;
};

async function* postPlanActionStream(
  conversationId: string,
  planId: string,
  action: 'answer' | 'approve' | 'reject',
  body: Record<string, unknown>,
  options: PlanActionStreamOptions = {},
): AsyncGenerator<TranscriptPatch, void> {
  const started = perfNow();
  const response = await leaseGuardedFetch(
    `/conversations/${encodeURIComponent(conversationId)}/plans/${encodeURIComponent(planId)}/${action}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: options.signal,
    },
  );
  recordSpan('stream.fetch', started, {
    conversation_id: conversationId,
    plan_id: planId,
    route: `plans.${action}`,
  });
  yield* parseTranscriptPatchSseResponse(response, {
    conversation_id: conversationId,
    plan_id: planId,
    route: `plans.${action}`,
  });
}

export const messageApi = {
  startRun: async (
    conversationId: string,
    data: SendMessageRequest,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<MessageRunStartResponse> => {
    const submit = () => apiClient.post<MessageRunStartResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/messages/runs`,
      data,
      { headers: { 'Idempotency-Key': idempotencyKey }, signal },
    );
    try {
      return (await submit()).data;
    } catch (error) {
      if (!(error instanceof ChatTreeApiError) || !error.retryable || signal?.aborted) {
        throw error;
      }
      return (await submit()).data;
    }
  },

  getActiveStreams: async (conversationId: string): Promise<ActiveStreamInfo[]> => {
    const response = await apiClient.get('/runs/active', { params: { conversation_id: conversationId } });
    return response.data;
  },

  getAllActiveStreams: async (): Promise<ActiveStreamInfo[]> => {
    const response = await apiClient.get('/runs/active');
    return response.data;
  },

  attachStream: async function* (
    conversationId: string,
    nodeId: string,
    options: MessageAttachStreamOptions,
  ): AsyncGenerator<TranscriptPatch, void> {
    const { signal } = options;
    const active = await messageApi.getActiveStreams(conversationId);
    const run = active.find((item) => item.target_node_id === nodeId || item.node_id === nodeId);
    if (!run?.run_id) {
      throw unexpectedApiResponse(404, new Error('No active run for node'));
    }
    yield* runsApi.attach(run.run_id, { signal });
  },

  answerPlanQuestion: async function* (
    conversationId: string,
    planId: string,
    answers: string[],
    options: PlanActionStreamOptions = {},
  ): AsyncGenerator<TranscriptPatch, void> {
    yield* postPlanActionStream(conversationId, planId, 'answer', { answers }, options);
  },

  approvePlan: async function* (
    conversationId: string,
    planId: string,
    options: PlanActionStreamOptions = {},
  ): AsyncGenerator<TranscriptPatch, void> {
    yield* postPlanActionStream(conversationId, planId, 'approve', {}, options);
  },

  rejectPlan: async function* (
    conversationId: string,
    planId: string,
    feedback = '',
    options: PlanActionStreamOptions = {},
  ): AsyncGenerator<TranscriptPatch, void> {
    yield* postPlanActionStream(conversationId, planId, 'reject', { feedback }, options);
  },

  approveTool: async (
    conversationId: string,
    toolCallId: string,
    nodeId: string,
    scope: 'once' | 'session' = 'once',
  ): Promise<ToolApprovalDecisionResponse> => {
    const response = await apiClient.post<ToolApprovalDecisionResponse>(
      `/tool-approvals/tool-calls/${encodeURIComponent(toolCallId)}/decide`,
      { decision: 'approve', conversation_id: conversationId, node_id: nodeId, scope },
    );
    return response.data;
  },

  rejectTool: async (
    conversationId: string,
    toolCallId: string,
    nodeId: string,
  ): Promise<ToolApprovalDecisionResponse> => {
    const response = await apiClient.post<ToolApprovalDecisionResponse>(
      `/tool-approvals/tool-calls/${encodeURIComponent(toolCallId)}/decide`,
      { decision: 'deny', conversation_id: conversationId, node_id: nodeId, scope: 'once' },
    );
    return response.data;
  },

  // 读取持久化工具结果切片
  getToolResult: async (
    toolResultId: string,
    offset = 0,
    limit = 16000,
  ): Promise<ToolResultSlice> => {
    const response = await apiClient.get(`/tool-results/${encodeURIComponent(toolResultId)}`, {
      params: { offset, limit },
    });
    return response.data;
  },

  // 回退写文件工具造成的变更
  revertToolResult: async (
    toolResultId: string,
  ): Promise<{ tool_result_id: string; reverted: string[] }> => {
    const response = await apiClient.post(`/tool-results/${encodeURIComponent(toolResultId)}/revert`);
    return response.data;
  },

};
