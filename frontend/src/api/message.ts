import { apiClient, serverApiUrl } from './client';
import type {
  Message,
  SendMessageRequest,
  StreamChunk,
  ToolApprovalDecision,
  ToolApprovalPayload,
  ToolApprovalScope,
} from '../types/message';
import { perfNow, recordMark, recordSpan } from '../perf/marks';

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

export type PlanActionStreamRequest = {
  node_id?: string | null;
  model_id?: string | null;
  provider_id?: string | null;
  reasoning_effort?: string | null;
  thinking_enabled?: boolean | null;
  tool_permission_mode?: string | null;
};

export type PlanAnswerStreamRequest = PlanActionStreamRequest & {
  answer: string;
};

export type PlanRejectStreamRequest = PlanActionStreamRequest & {
  feedback: string;
};

export type PendingToolApprovalsResponse = {
  approvals: ToolApprovalPayload[];
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

async function* parseSseResponse(
  response: Response,
  perfAttrs: Record<string, unknown> = {},
): AsyncGenerator<StreamChunk, void> {
  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`);
  }

  recordMark('stream.response_headers', {
    ...perfAttrs,
    status: response.status,
  });
  const reader = response.body?.getReader();
  const decoder = new TextDecoder();

  if (!reader) {
    throw new Error('Response body is not readable');
  }

  let buffer = '';
  let firstChunk = true;
  let eventCount = 0;

  try {
    while (true) {
      const readStarted = perfNow();
      const { done, value } = await reader.read();
      recordSpan('stream.reader_read', readStarted, {
        ...perfAttrs,
        done,
        bytes: value?.byteLength ?? 0,
      });
      if (done) break;
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

        if (trimmed.startsWith('data: ')) {
          const jsonData = trimmed.slice(6);
          if (jsonData === '[DONE]') {
            recordMark('stream.done', { ...perfAttrs, event_count: eventCount });
            return;
          }
          try {
            const parseStarted = perfNow();
            const parsed: StreamChunk = JSON.parse(jsonData);
            eventCount += 1;
            recordSpan('stream.parse_event', parseStarted, {
              ...perfAttrs,
              event_type: (parsed as any).event_type,
              status: (parsed as any).status,
              run_id: (parsed as any).run_id,
              event_index: (parsed as any).event_index,
            });
            yield parsed;
          } catch (e) {
            recordMark('stream.parse_error', { ...perfAttrs });
            console.error('Failed to parse stream chunk:', e, jsonData);
          }
        }
      }
    }

    if (buffer.trim()) {
      const trimmed = buffer.trim();
      if (trimmed.startsWith('data: ')) {
        const jsonData = trimmed.slice(6);
        if (jsonData !== '[DONE]') {
          try {
            const parseStarted = perfNow();
            const parsed: StreamChunk = JSON.parse(jsonData);
            eventCount += 1;
            recordSpan('stream.parse_event', parseStarted, {
              ...perfAttrs,
              event_type: (parsed as any).event_type,
              status: (parsed as any).status,
              run_id: (parsed as any).run_id,
              event_index: (parsed as any).event_index,
            });
            yield parsed;
          } catch (e) {
            recordMark('stream.parse_error', { ...perfAttrs, final_buffer: true });
            console.error('Failed to parse final stream chunk:', e, jsonData);
          }
        }
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch (_) {
      // reader 可能已关闭，忽略
    }
  }
}

export const messageApi = {
  // 流式发送消息
  stream: async function* (
    conversationId: string,
    data: SendMessageRequest,
    nodeId?: string,
    signal?: AbortSignal
  ): AsyncGenerator<StreamChunk, void> {
    const started = perfNow();
    const response = await fetch(serverApiUrl(`/conversations/${conversationId}/messages/stream`), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ...data, parent_node_id: nodeId ?? data.parent_node_id, focus_new_node: data.focus_new_node ?? true }),
      signal,
    });
    recordSpan('stream.fetch', started, {
      conversation_id: conversationId,
      route: 'messages.stream',
    });

    yield* parseSseResponse(response, {
      conversation_id: conversationId,
      route: 'messages.stream',
    });
  },

  getActiveStreams: async (conversationId: string): Promise<ActiveStreamInfo[]> => {
    const response = await apiClient.get(`/conversations/${conversationId}/messages/streams/active`);
    return response.data;
  },

  getAllActiveStreams: async (): Promise<ActiveStreamInfo[]> => {
    const response = await apiClient.get('/conversations/messages/streams/active');
    return response.data;
  },

  attachStream: async function* (
    conversationId: string,
    nodeId: string,
    fromEvent = 0,
    signal?: AbortSignal,
  ): AsyncGenerator<StreamChunk, void> {
    const started = perfNow();
    const response = await fetch(
      serverApiUrl(`/conversations/${conversationId}/messages/${nodeId}/stream/attach?from_event=${fromEvent}`),
      { signal },
    );
    recordSpan('stream.fetch', started, {
      conversation_id: conversationId,
      node_id: nodeId,
      from_event: fromEvent,
      route: 'messages.attach',
    });
    yield* parseSseResponse(response, {
      conversation_id: conversationId,
      node_id: nodeId,
      from_event: fromEvent,
      route: 'messages.attach',
    });
  },

  streamPlanApproval: async function* (
    conversationId: string,
    planId: string,
    data: PlanActionStreamRequest,
    signal?: AbortSignal,
  ): AsyncGenerator<StreamChunk, void> {
    const started = perfNow();
    const response = await fetch(
      serverApiUrl(`/conversations/${encodeURIComponent(conversationId)}/plans/${encodeURIComponent(planId)}/approve/stream`),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
        signal,
      },
    );
    recordSpan('stream.fetch', started, { conversation_id: conversationId, plan_id: planId, route: 'plans.approve' });
    yield* parseSseResponse(response, { conversation_id: conversationId, plan_id: planId, route: 'plans.approve' });
  },

  streamPlanAnswer: async function* (
    conversationId: string,
    planId: string,
    data: PlanAnswerStreamRequest,
    signal?: AbortSignal,
  ): AsyncGenerator<StreamChunk, void> {
    const started = perfNow();
    const response = await fetch(
      serverApiUrl(`/conversations/${encodeURIComponent(conversationId)}/plans/${encodeURIComponent(planId)}/answer/stream`),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
        signal,
      },
    );
    recordSpan('stream.fetch', started, { conversation_id: conversationId, plan_id: planId, route: 'plans.answer' });
    yield* parseSseResponse(response, { conversation_id: conversationId, plan_id: planId, route: 'plans.answer' });
  },

  streamPlanReject: async function* (
    conversationId: string,
    planId: string,
    data: PlanRejectStreamRequest,
    signal?: AbortSignal,
  ): AsyncGenerator<StreamChunk, void> {
    const started = perfNow();
    const response = await fetch(
      serverApiUrl(`/conversations/${encodeURIComponent(conversationId)}/plans/${encodeURIComponent(planId)}/reject/stream`),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
        signal,
      },
    );
    recordSpan('stream.fetch', started, { conversation_id: conversationId, plan_id: planId, route: 'plans.reject' });
    yield* parseSseResponse(response, { conversation_id: conversationId, plan_id: planId, route: 'plans.reject' });
  },

  // 获取消息历史
  getHistory: async (conversationId: string): Promise<Message[]> => {
    const response = await apiClient.get(`/conversations/${conversationId}/messages`);
    return response.data;
  },

  // 停止流式消息生成
  stopStream: async (conversationId: string, nodeId: string): Promise<void> => {
    await apiClient.post(`/conversations/${conversationId}/messages/${nodeId}/stream/stop`);
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

  getPendingApprovals: async (
    conversationId?: string | null,
  ): Promise<ToolApprovalPayload[]> => {
    const response = await apiClient.get<PendingToolApprovalsResponse>('/tool-approvals/pending', {
      params: conversationId ? { conversation_id: conversationId } : undefined,
    });
    return response.data.approvals || [];
  },

  // 决定工具审批请求
  decideApproval: async (
    approvalId: string,
    decision: ToolApprovalDecision,
    scope: ToolApprovalScope = 'once',
  ): Promise<void> => {
    await apiClient.post(`/tool-approvals/${encodeURIComponent(approvalId)}/decide`, {
      decision,
      scope,
      remember_rule: false,
    });
  },
};
