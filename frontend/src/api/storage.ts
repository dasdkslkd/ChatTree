import { apiClient } from './client';

export type StorageStats = {
  db_file_bytes: number;
  logical_bytes: number;
  freelist_bytes: number;
  blobs_bytes: number;
  blobs_count: number;
  home: string;
  conversations_dir_bytes: number;
  run_journals_count: number;
  active_runs: number;
  reclaimable_bytes: number;
  recommended: boolean;
};

export type CompactResponse = {
  reclaimed_blobs: number;
  db_file_bytes_after: number;
  status: string;
};

export const storageApi = {
  stats: async (): Promise<StorageStats> => {
    const response = await apiClient.get('/storage/stats');
    return response.data;
  },
  compact: async (): Promise<CompactResponse> => {
    const response = await apiClient.post('/storage/compact', {}, { timeout: 120000 });
    return response.data;
  },
};