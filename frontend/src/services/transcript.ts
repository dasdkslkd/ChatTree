import { apiClient } from '../api/client';
import type { TranscriptSnapshot } from '../types/transcript';
import type { AxiosInstance } from 'axios';

type TranscriptApiClient = Pick<AxiosInstance, 'get'>;

export function createTranscriptService(client: TranscriptApiClient) {
  return {
    async fetchBranchSnapshot(
      conversationId: string,
      tipNodeId: string,
      signal?: AbortSignal,
    ): Promise<TranscriptSnapshot> {
      const response = await client.get<TranscriptSnapshot>(
        `/conversations/${encodeURIComponent(conversationId)}/transcript`,
        { ...(signal ? { signal } : {}), params: { node_id: tipNodeId } },
      );
      return response.data;
    },
  };
}

export const transcriptService = createTranscriptService(apiClient);
