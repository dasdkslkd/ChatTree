import { apiClient } from './client';
import type { ConfigData, ConfigUpdateRequest } from '../types/model';

export const configApi = {
  // 获取配置
  get: async (): Promise<ConfigData> => {
    const response = await apiClient.get('/config');
    return response.data;
  },

  // 更新配置
  update: async (data: ConfigUpdateRequest): Promise<{ message: string }> => {
    const response = await apiClient.put('/config', data);
    return response.data;
  },
};