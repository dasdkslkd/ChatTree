import { apiClient } from './client';
import type { ModelProvider } from '../types/model';

export const modelApi = {
  // 获取模型列表
  list: async (provider: ModelProvider): Promise<string[]> => {
    const response = await apiClient.get(`/models/${provider}`);
    return response.data;
  },

  // 获取提供商列表
  getProviders: async (): Promise<ModelProvider[]> => {
    const response = await apiClient.get('/models');
    return response.data;
  },
};