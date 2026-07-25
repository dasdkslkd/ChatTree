import { apiClient } from './client';
import type { SendMessageRequest } from '../types/message';
import type { TranscriptPatch } from '../types/transcript';
import { perfNow, recordMark, recordSpan } from '../perf/marks';
import { leaseGuardedFetch } from './leaseFetch';
import { runsApi } from './runs';
import type { RunStartResponse } from './runs';
import {
  apiErrorFromResponse,
  ChatTreeApiError,
  normalizeFetchError,
  unexpectedApiResponse,
} from './errors';

export type ToolResultSlice = {
  tool_result_id: string;
  tool_name?: string | null;
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

export type MessageStreamOptions = {
  signal?: AbortSignal;
  nodeId?: string;
  idempotencyKey?: string;
};

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

async function acquireSseReader(
  response: Response,
): Promise<ReadableStreamDefaultReader<Uint8Array>> {
  try {
    if (!response.ok) {
      throw await apiErrorFromResponse(response);
    }
    const reader = response.body?.getReader();
    if (!reader) {
      throw unexpectedApiResponse(
        response.status,
        new Error('Response body is not readable'),
      );
    }
    return reader;
  } catch (error) {
    try {
      await response.body?.cancel();
    } catch {
      // The transport may already have closed or cancelled the body.
    }
    throw normalizeFetchError(error);
  }
}

async function* parseSseResponse(
  response: Response,
  perfAttrs: Record<string, unknown> = {},
): AsyncGenerator<TranscriptPatch, void> {
  const reader = await acquireSseReader(response);
  recordMark('stream.response_headers', {
    ...perfAttrs,
    status: response.status,
  });
  const decoder = new TextDecoder();

  let buffer = '';
  let firstChunk = true;
  let eventCount = 0;

  try {
    while (true) {
      const readStarted = perfNow();
      let readResult: ReadableStreamReadResult<Uint8Array>;
      try {
        readResult = await reader.read();
      } catch (error) {
        throw normalizeFetchError(error);
      }
      const { done, value } = readResult;
      recordSpan('stream.reader_read', readStarted, {
        ...perfAttrs,
        done,
        bytes: value?.byteLength ?? 0,
      });
      if (done) {
        buffer += decoder.decode();
        break;
      }
      if (firstChunk) {
        recordMark('stream.first_bytes', {
          ...perfAttrs,
          bytes: value?.byteLength ?? 0,
        });
        firstChunk = false;
      }

      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';

      for (const part of parts) {
        const trimmed = part.trim();
        if (!trimmed) continue;

        if (trimmed.startsWith('data:')) {
          const jsonData = trimmed.slice(5).trimStart();
          if (jsonData === '[DONE]') {
            recordMark('stream.done', { ...perfAttrs, event_count: eventCount });
            return;
          }
          try {
            const parseStarted = perfNow();
            const parsed = JSON.parse(jsonData) as TranscriptPatch;
            eventCount += 1;
            recordSpan('stream.parse_event', parseStarted, {
              ...perfAttrs,
              event_type: parsed.type,
              revision: parsed.revision,
              operation_count: parsed.operations.length,
            });
            yield parsed;
          } catch (e) {
            recordMark('stream.parse_error', { ...perfAttrs });
            throw unexpectedApiResponse(response.status, e);
          }
        }
      }
    }

    if (buffer.trim()) {
      const trimmed = buffer.trim();
      if (trimmed.startsWith('data:')) {
        const jsonData = trimmed.slice(5).trimStart();
        if (jsonData === '[DONE]') {
          recordMark('stream.done', { ...perfAttrs, event_count: eventCount });
          return;
        }
        try {
          const parseStarted = perfNow();
          const parsed = JSON.parse(jsonData) as TranscriptPatch;
          eventCount += 1;
          recordSpan('stream.parse_event', parseStarted, {
            ...perfAttrs,
            event_type: parsed.type,
            revision: parsed.revision,
            operation_count: parsed.operations.length,
          });
          yield parsed;
        } catch (e) {
          recordMark('stream.parse_error', { ...perfAttrs, final_buffer: true });
          throw unexpectedApiResponse(response.status, e);
        }
      }
    }
    throw unexpectedApiResponse(
      response.status,
      new Error('SSE stream ended before data:[DONE]'),
    );
  } finally {
    try {
      await reader.cancel();
    } catch {
      // The reader may already be closed or cancelled.
    }
  }
}

async function* parseTranscriptPatchSseResponse(
  response: Response,
  perfAttrs: Record<string, unknown> = {},
): AsyncGenerator<TranscriptPatch, void> {
  for await (const chunk of parseSseResponse(response, perfAttrs)) {
    const payload = chunk as { type?: unknown };
    if (payload?.type !== 'transcript_patch') {
      throw unexpectedApiResponse(
        response.status,
        new Error('Plan action stream returned a non transcript_patch event'),
      );
    }
    yield chunk as unknown as TranscriptPatch;
  }
}

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
    const response = await apiClient.post<MessageRunStartResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/messages/runs`,
      data,
      {
        headers: { 'Idempotency-Key': idempotencyKey },
        signal,
      },
    );
    return response.data;
  },

  // Start is idempotent; attach is a separate replayable GET.
  stream: async function* (
    conversationId: string,
    data: SendMessageRequest,
    options: MessageStreamOptions,
  ): AsyncGenerator<TranscriptPatch, void> {
    const { nodeId, signal } = options;
    const idempotencyKey = options.idempotencyKey ?? (
      typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `message-${Date.now()}-${Math.random().toString(36).slice(2)}`
    );
    const payload = {
      ...data,
      parent_node_id: nodeId ?? data.parent_node_id,
      focus_new_node: data.focus_new_node ?? true,
    };
    const started = perfNow();
    let start: MessageRunStartResponse;
    try {
      start = await messageApi.startRun(conversationId, payload, idempotencyKey, signal);
    } catch (error) {
      if (!(error instanceof ChatTreeApiError) || !error.retryable || signal?.aborted) {
        throw error;
      }
      start = await messageApi.startRun(conversationId, payload, idempotencyKey, signal);
    }
    recordSpan('stream.fetch', started, {
      conversation_id: conversationId,
      run_id: start.run_id,
      route: 'messages.start',
    });
    yield* runsApi.attach(start.run_id, { signal });
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
    answer: string,
    options: PlanActionStreamOptions = {},
  ): AsyncGenerator<TranscriptPatch, void> {
    yield* postPlanActionStream(conversationId, planId, 'answer', { answer }, options);
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

};
