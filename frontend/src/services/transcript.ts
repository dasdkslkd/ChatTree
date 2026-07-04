import { apiClient } from '../api/client';
import type { TranscriptItem } from '../types/transcript';

type TranscriptApiClient = {
  get: (url: string, config?: any) => Promise<{ data: { items?: TranscriptItem[] } }>;
};

export function createTranscriptService(client: TranscriptApiClient) {
  return {
    async fetchTranscript(conversationId: string, nodeId?: string | null): Promise<TranscriptItem[]> {
      const params = nodeId ? { node_id: nodeId } : undefined;
      const response = await client.get(
        `/conversations/${encodeURIComponent(conversationId)}/transcript`,
        params ? { params } : undefined,
      );
      return response.data.items || [];
    },
  };
}

export const transcriptService = createTranscriptService(apiClient);
