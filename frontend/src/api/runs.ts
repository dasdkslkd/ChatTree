import { apiClient } from './client';
import type { StreamChunk } from '../types/message';
import type { RunEventPayload, RunRecord } from '../types/run';
import { perfNow, recordMark, recordSpan } from '../perf/marks';
import {
  connectionEpochRuntime,
  StaleConnectionEpochError,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';
import { leaseGuardedFetch } from './leaseFetch';

export type RunAttachOptions = {
  token: ConnectionEpochToken;
  signal?: AbortSignal;
  fromEvent?: number;
};

function assertStreamCurrent(token: ConnectionEpochToken): void {
  if (!connectionEpochRuntime.isCurrent(token)) {
    throw new StaleConnectionEpochError();
  }
}

async function acquireSseReader(
  response: Response,
  token: ConnectionEpochToken,
): Promise<ReadableStreamDefaultReader<Uint8Array>> {
  try {
    assertStreamCurrent(token);
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error('Response body is not readable');
    return reader;
  } catch (error) {
    try {
      await response.body?.cancel();
    } catch {
      // The transport may already have closed or cancelled the body.
    }
    throw error;
  }
}

async function* parseSseResponse(
  response: Response,
  token: ConnectionEpochToken,
  perfAttrs: Record<string, unknown> = {},
): AsyncGenerator<StreamChunk, void> {
  const reader = await acquireSseReader(response, token);
  recordMark('stream.response_headers', { ...perfAttrs, status: response.status });
  const decoder = new TextDecoder();
  let buffer = '';
  let eventCount = 0;
  let firstChunk = true;
  try {
    while (true) {
      const readStarted = perfNow();
      let readResult: ReadableStreamReadResult<Uint8Array>;
      try {
        readResult = await reader.read();
      } catch (error) {
        assertStreamCurrent(token);
        throw error;
      }
      assertStreamCurrent(token);
      const { done, value } = readResult;
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
        assertStreamCurrent(token);
        yield parsed;
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // The reader may already be closed or cancelled.
    }
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

  get: async (runId: string): Promise<RunRecord> => {
    const response = await apiClient.get(`/runs/${encodeURIComponent(runId)}`);
    return response.data;
  },

  attach: async function* (
    runId: string,
    options: RunAttachOptions,
  ): AsyncGenerator<StreamChunk, void> {
    const { token, signal } = options;
    const fromEvent = options.fromEvent ?? 0;
    const started = perfNow();
    const response = await leaseGuardedFetch(
      `/runs/${encodeURIComponent(runId)}/attach?from_event=${fromEvent}`,
      { signal },
      token,
    );
    recordSpan('stream.fetch', started, { run_id: runId, from_event: fromEvent, route: 'runs.attach' });
    yield* parseSseResponse(response, token, { run_id: runId, from_event: fromEvent, route: 'runs.attach' });
  },

  stop: async (runId: string): Promise<void> => {
    await apiClient.post(`/runs/${encodeURIComponent(runId)}/stop`);
  },

  stopConversation: async (conversationId: string): Promise<{ run_ids: string[] }> => {
    const response = await apiClient.post(`/conversations/${conversationId}/runs/stop`);
    return response.data;
  },

  events: async (runId: string, fromEvent = 0): Promise<RunEventPayload[]> => {
    const response = await apiClient.get(`/runs/${encodeURIComponent(runId)}/events`, { params: { from_event: fromEvent } });
    return response.data;
  },
};
