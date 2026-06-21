import { apiClient } from './client';
import type {
  Message,
  SendMessageRequest,
  StreamChunk,
  ToolApprovalDecision,
  ToolApprovalScope,
} from '../types/message';

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

export const messageApi = {
  // 流式发送消息
  stream: async function* (
    conversationId: string,
    data: SendMessageRequest,
    nodeId?: string,
    signal?: AbortSignal
  ): AsyncGenerator<StreamChunk, void> {
    const response = await fetch(`/api/conversations/${conversationId}/messages/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ...data, node_id: nodeId }),
      signal,
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const reader = response.body?.getReader();
    const decoder = new TextDecoder();

    if (!reader) {
      throw new Error('Response body is not readable');
    }

    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // 按双换行符分割 SSE 消息
        const parts = buffer.split('\n\n');
        // 最后一部分可能不完整，保留到下次处理
        buffer = parts.pop() || '';

        for (const part of parts) {
          const trimmed = part.trim();
          if (!trimmed) continue;

          if (trimmed.startsWith('data: ')) {
            const jsonData = trimmed.slice(6);
            if (jsonData === '[DONE]') {
              return;
            }
            try {
              const parsed: StreamChunk = JSON.parse(jsonData);
              yield parsed;
            } catch (e) {
              console.error('Failed to parse stream chunk:', e, jsonData);
            }
          }
        }
      }

      // 处理剩余的buffer
      if (buffer.trim()) {
        const trimmed = buffer.trim();
        if (trimmed.startsWith('data: ')) {
          const jsonData = trimmed.slice(6);
          if (jsonData !== '[DONE]') {
            try {
              const parsed: StreamChunk = JSON.parse(jsonData);
              yield parsed;
            } catch (e) {
              console.error('Failed to parse final stream chunk:', e, jsonData);
            }
          }
        }
      }
    } finally {
      // 无论正常结束、提前 return（[DONE]）还是因 abort 抛出，
      // 都要释放底层连接，避免并发时连接泄漏。
      try {
        await reader.cancel();
      } catch (_) {
        // reader 可能已关闭，忽略
      }
    }
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
