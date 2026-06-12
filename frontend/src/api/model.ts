import { apiClient } from './client';

export const modelApi = {
  // 获取指定提供商的模型列表
  list: async (provider: string): Promise<string[]> => {
    const response = await apiClient.get(`/models/${provider}`);
    return response.data;
  },

  // 获取已配置的提供商列表
  getProviders: async (): Promise<string[]> => {
    const response = await apiClient.get('/models');
    return response.data;
  },
};
