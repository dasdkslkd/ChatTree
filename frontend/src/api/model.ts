import { apiClient } from './client';
import type { ModelMetadata } from '../types/model';

export const modelApi = {
  // 获取指定提供商下所有模型的元数据（model_name -> 元数据）
  metadata: async (provider: string): Promise<Record<string, ModelMetadata>> => {
    const response = await apiClient.get(`/models/${provider}/metadata`);
    return response.data;
  },
};
