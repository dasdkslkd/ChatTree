import { apiClient } from './client';
import type { MemoryViewResponse } from '../types/model';

export const memoryApi = {
  get: async (projectId?: string): Promise<MemoryViewResponse> => {
    const response = await apiClient.get('/memory', {
      params: projectId ? { project_id: projectId } : undefined,
    });
    return response.data;
  },
};
