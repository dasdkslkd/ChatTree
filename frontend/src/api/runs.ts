import { apiClient } from './client';
import type { StreamChunk } from '../types/message';
import type { RunEventPayload, RunRecord } from '../types/run';

async function* parseSseResponse(response: Response): AsyncGenerator<StreamChunk, void> {
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  const reader = response.body?.getReader();
  if (!reader) throw new Error('Response body is not readable');
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';
      for (const part of parts) {
        const trimmed = part.trim();
        if (!trimmed.startsWith('data: ')) continue;
        const data = trimmed.slice(6);
        if (data === '[DONE]') return;
        yield JSON.parse(data);
      }
    }
  } finally {
    try { await reader.cancel(); } catch (_) {}
  }
}

export const runsApi = {
  listActive: async (conversationId?: string): Promise<RunRecord[]> => {
    const response = await apiClient.get('/runs/active', { params: conversationId ? { conversation_id: conversationId } : undefined });
    return response.data;
  },

  listConversation: async (conversationId: string): Promise<RunRecord[]> => {
    const response = await apiClient.get(`/conversations/${conversationId}/runs`);
    return response.data;
  },

  attach: async function* (runId: string, fromEvent = 0, signal?: AbortSignal): AsyncGenerator<StreamChunk, void> {
    const response = await fetch(`/api/runs/${runId}/attach?from_event=${fromEvent}`, { signal });
    yield* parseSseResponse(response);
  },

  stop: async (runId: string): Promise<void> => {
    await apiClient.post(`/runs/${runId}/stop`);
  },

  stopConversation: async (conversationId: string): Promise<{ run_ids: string[] }> => {
    const response = await apiClient.post(`/conversations/${conversationId}/runs/stop`);
    return response.data;
  },

  events: async (runId: string, fromEvent = 0): Promise<RunEventPayload[]> => {
    const response = await apiClient.get(`/runs/${runId}/events`, { params: { from_event: fromEvent } });
    return response.data;
  },
};
