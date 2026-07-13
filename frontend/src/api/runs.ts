import { apiClient } from './client';
import type { StreamChunk } from '../types/message';
import type { RunEventPayload, RunRecord } from '../types/run';
import { perfNow, recordMark, recordSpan } from '../perf/marks';

async function* parseSseResponse(
  response: Response,
  perfAttrs: Record<string, unknown> = {},
): AsyncGenerator<StreamChunk, void> {
  if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
  recordMark('stream.response_headers', { ...perfAttrs, status: response.status });
  const reader = response.body?.getReader();
  if (!reader) throw new Error('Response body is not readable');
  const decoder = new TextDecoder();
  let buffer = '';
  let eventCount = 0;
  let firstChunk = true;
  try {
    while (true) {
      const readStarted = perfNow();
      const { done, value } = await reader.read();
      recordSpan('stream.reader_read', readStarted, { ...perfAttrs, done, bytes: value?.byteLength ?? 0 });
      if (done) break;
      if (firstChunk) {
        firstChunk = false;
        recordMark('stream.first_bytes', { ...perfAttrs, bytes: value?.byteLength ?? 0 });
      }
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';
      for (const part of parts) {
        const trimmed = part.trim();
        if (!trimmed.startsWith('data: ')) continue;
        const data = trimmed.slice(6);
        if (data === '[DONE]') {
          recordMark('stream.done', { ...perfAttrs, event_count: eventCount });
          return;
        }
        const parseStarted = perfNow();
        const parsed = JSON.parse(data);
        eventCount += 1;
        recordSpan('stream.parse_event', parseStarted, {
          ...perfAttrs,
          event_type: parsed?.event_type,
          status: parsed?.status,
          run_id: parsed?.run_id,
          event_index: parsed?.event_index,
        });
        yield parsed;
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
    const started = perfNow();
    const response = await fetch(`/api/runs/${runId}/attach?from_event=${fromEvent}`, { signal });
    recordSpan('stream.fetch', started, { run_id: runId, from_event: fromEvent, route: 'runs.attach' });
    yield* parseSseResponse(response, { run_id: runId, from_event: fromEvent, route: 'runs.attach' });
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
