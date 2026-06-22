import { apiClient } from './client';
import type { ConfigData, ConfigUpdateRequest, AddProviderRequest, ToolInventoryStatus, CapabilityInventory } from '../types/model';

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

  // 添加提供商
  addProvider: async (data: AddProviderRequest): Promise<{ message: string }> => {
    const response = await apiClient.post('/config/providers', data);
    return response.data;
  },

  // 删除提供商
  deleteProvider: async (providerId: string): Promise<{ message: string }> => {
    const response = await apiClient.delete(`/config/providers/${providerId}`);
    return response.data;
  },

  getMcpStatus: async (): Promise<ToolInventoryStatus> => {
    const response = await apiClient.get('/tools/mcp/status');
    return response.data;
  },

  getCapabilities: async (): Promise<CapabilityInventory> => {
    const response = await apiClient.get('/capabilities');
    return response.data;
  },

  connectMcpServer: async (serverName: string): Promise<ToolInventoryStatus> => {
    const response = await apiClient.post(`/tools/mcp/servers/${encodeURIComponent(serverName)}/connect`);
    return response.data;
  },
};
