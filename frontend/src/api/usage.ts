import { apiClient } from './client';

export type UsageRange = '1d' | '7d' | '30d' | '1y' | 'total';

export type ModelUsage = {
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_hit_tokens: number;
  cache_hit_rate: number | null;
};

export type UsageTotals = {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_hit_tokens: number;
  cache_context_tokens: number;
  cache_hit_rate: number | null;
};

export type UsageStats = {
  models: ModelUsage[];
  totals: UsageTotals;
};

export const usageApi = {
  stats: async (period: UsageRange = '1d'): Promise<UsageStats> => {
    const response = await apiClient.get('/usage/stats', { params: { period } });
    return response.data;
  },
};