import { apiClient } from './client';
import type { ModelMetadata } from '../types/model';

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

  // 获取指定提供商下所有模型的元数据（model_name -> 元数据）
  metadata: async (provider: string): Promise<Record<string, ModelMetadata>> => {
    const response = await apiClient.get(`/models/${provider}/metadata`);
    return response.data;
  },
};
