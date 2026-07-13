import { apiClient } from '../api/client';
import type { TranscriptSnapshot } from '../types/transcript';

type TranscriptApiClient = {
  get: (url: string, config?: any) => Promise<{ data: TranscriptSnapshot }>;
};

export function createTranscriptService(client: TranscriptApiClient) {
  return {
    async fetchBranchSnapshot(
      conversationId: string,
      tipNodeId: string,
      signal?: AbortSignal,
    ): Promise<TranscriptSnapshot> {
      const response = await client.get(
        `/conversations/${encodeURIComponent(conversationId)}/branches/${encodeURIComponent(tipNodeId)}/transcript`,
        signal ? { signal } : undefined,
      );
      return response.data;
    },
  };
}

export const transcriptService = createTranscriptService(apiClient);
