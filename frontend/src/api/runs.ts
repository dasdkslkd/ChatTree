import { apiClient } from './client';
import type { TranscriptPatch } from '../types/transcript';
import type { RunRecord } from '../types/run';
import { perfNow, recordMark, recordSpan } from '../perf/marks';
import { leaseGuardedFetch } from './leaseFetch';
import {
  apiErrorFromResponse,
  normalizeFetchError,
  unexpectedApiResponse,
} from './errors';

export type RunStartResponse = {
  run_id: string;
  created: boolean;
  status: string;
};

export type RunAttachOptions = {
  signal?: AbortSignal;
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
        throw normalizeFetchError(error);
      }
      const { done, value } = readResult;
      recordSpan('stream.reader_read', readStarted, { ...perfAttrs, done, bytes: value?.byteLength ?? 0 });
      if (done) {
        buffer += decoder.decode();
        break;
      }
      if (firstChunk) {
        firstChunk = false;
        recordMark('stream.first_bytes', { ...perfAttrs, bytes: value?.byteLength ?? 0 });
      }
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';
      for (const part of parts) {
        const trimmed = part.trim();
        if (!trimmed.startsWith('data:')) continue;
        const data = trimmed.slice(5).trimStart();
        if (data === '[DONE]') {
          recordMark('stream.done', { ...perfAttrs, event_count: eventCount });
          return;
        }
        const parseStarted = perfNow();
        let parsed: TranscriptPatch;
        try {
          parsed = JSON.parse(data);
        } catch (error) {
          recordMark('stream.parse_error', { ...perfAttrs });
          throw unexpectedApiResponse(response.status, error);
        }
        eventCount += 1;
        recordSpan('stream.parse_event', parseStarted, {
          ...perfAttrs,
          event_type: parsed?.type,
          revision: parsed?.revision,
          operation_count: parsed?.operations?.length ?? 0,
        });
        yield parsed;
      }
    }

    const finalPart = buffer.trim();
    if (finalPart.startsWith('data:')) {
      const data = finalPart.slice(5).trimStart();
      if (data === '[DONE]') {
        recordMark('stream.done', { ...perfAttrs, event_count: eventCount });
        return;
      }
      try {
        const parsed = JSON.parse(data) as TranscriptPatch;
        eventCount += 1;
        yield parsed;
      } catch (error) {
        recordMark('stream.parse_error', { ...perfAttrs, final_buffer: true });
        throw unexpectedApiResponse(response.status, error);
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

export async function* parseTranscriptPatchSseResponse(
  response: Response,
  perfAttrs: Record<string, unknown> = {},
): AsyncGenerator<TranscriptPatch, void> {
  for await (const chunk of parseSseResponse(response, perfAttrs)) {
    const payload = chunk as { type?: unknown };
    if (payload?.type !== 'transcript_patch') {
      throw unexpectedApiResponse(
        response.status,
        new Error('SSE stream returned a non transcript_patch event'),
      );
    }
    yield chunk as unknown as TranscriptPatch;
  }
}

export const runsApi = {
  get: async (runId: string): Promise<RunRecord> => {
    const response = await apiClient.get(`/runs/${encodeURIComponent(runId)}`);
    return response.data;
  },

  attach: async function* (
    runId: string,
    options: RunAttachOptions,
  ): AsyncGenerator<TranscriptPatch, void> {
    const { signal } = options;
    const started = perfNow();
    const response = await leaseGuardedFetch(
      `/runs/${encodeURIComponent(runId)}/events`,
      { signal },
    );
    recordSpan('stream.fetch', started, { run_id: runId, route: 'runs.events' });
    yield* parseTranscriptPatchSseResponse(response, { run_id: runId, route: 'runs.events' });
  },

  stop: async (runId: string): Promise<void> => {
    await apiClient.post(`/runs/${encodeURIComponent(runId)}/stop`);
  },

  observe: async (runId: string): Promise<RunRecord> => {
    const response = await apiClient.post<RunRecord>(`/runs/${encodeURIComponent(runId)}/observe`);
    return response.data;
  },

};
