import { apiClient } from './client';
import type { BuiltinWebStatus, ConfigData, ConfigUpdateRequest, AddProviderRequest, ToolInventoryStatus, CapabilityInventory, ProjectCapabilityConfig, ProjectSettingsResponse } from '../types/model';

// 订阅登录 handle（device code flow 返回）
export interface SubscriptionLoginHandle {
  subscription: string;
  verification_uri: string;
  user_code: string;
  interval: number;
  expires_at: number;
  [key: string]: unknown;
}

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

  // 检测系统 PATH 中的常用开发工具
  getDevEnvironmentDetected: async (): Promise<Record<string, string | null>> => {
    const response = await apiClient.get('/config/dev-environment/detected');
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

  // 启动订阅 OAuth 登录
  startSubscriptionLogin: async (
    providerId: string,
    subscription: string,
    enterpriseDomain?: string,
  ): Promise<SubscriptionLoginHandle> => {
    const response = await apiClient.post(
      `/config/providers/${encodeURIComponent(providerId)}/auth/login`,
      { subscription, enterprise_domain: enterpriseDomain },
    );
    return response.data;
  },

  // 轮询订阅登录结果
  pollSubscriptionLogin: async (
    providerId: string,
    subscription: string,
    handle: SubscriptionLoginHandle,
  ): Promise<{ status: 'ok' | 'pending'; auth?: unknown }> => {
    const response = await apiClient.post(
      `/config/providers/${encodeURIComponent(providerId)}/auth/poll`,
      { subscription, handle },
    );
    return response.data;
  },

  // 从 CLI 工具导入凭据
  importCliCredentials: async (
    providerId: string,
    subscription: string,
  ): Promise<{ status: string; auth?: unknown }> => {
    const response = await apiClient.post(
      `/config/providers/${encodeURIComponent(providerId)}/auth/cli-import`,
      { subscription },
    );
    return response.data;
  },

  // 查询订阅额度
  getProviderQuota: async (providerId: string): Promise<Record<string, unknown>> => {
    const response = await apiClient.get(
      `/config/providers/${encodeURIComponent(providerId)}/quota`,
    );
    return response.data;
  },

  // 强制刷新模型列表
  refreshProviderModels: async (providerId: string): Promise<{ models: string[] }> => {
    const response = await apiClient.post(
      `/config/providers/${encodeURIComponent(providerId)}/models/refresh`,
    );
    return response.data;
  },

  getMcpStatus: async (): Promise<ToolInventoryStatus> => {
    const response = await apiClient.get('/tools/mcp/status');
    return response.data;
  },

  getBuiltinWebStatus: async (): Promise<BuiltinWebStatus> => {
    const response = await apiClient.get('/tools/builtin/web/status');
    return response.data;
  },

  restartBuiltinWeb: async (): Promise<{ restarted: boolean }> => {
    const response = await apiClient.post('/tools/builtin/web/restart');
    return response.data;
  },

  getCapabilities: async (): Promise<CapabilityInventory> => {
    const response = await apiClient.get('/capabilities');
    return response.data;
  },

  reloadCapabilities: async (): Promise<CapabilityInventory> => {
    const response = await apiClient.post('/capabilities/reload');
    return response.data;
  },

  connectMcpServer: async (serverName: string): Promise<ToolInventoryStatus> => {
    const response = await apiClient.post(`/tools/mcp/servers/${encodeURIComponent(serverName)}/connect`);
    return response.data;
  },

  disconnectMcpServer: async (serverName: string): Promise<ToolInventoryStatus> => {
    const response = await apiClient.post(`/tools/mcp/servers/${encodeURIComponent(serverName)}/disconnect`);
    return response.data;
  },

  getProjects: async (): Promise<ProjectSettingsResponse> => {
    const response = await apiClient.get('/projects');
    return response.data;
  },

  updateProject: async (path: string, data: ProjectCapabilityConfig): Promise<{ message: string; project: ProjectCapabilityConfig }> => {
    const response = await apiClient.put(`/projects/${encodeURIComponent(path)}`, { ...data, path });
    return response.data;
  },

  deleteProjectHistory: async (path: string, force = false): Promise<{ project_path: string; deleted_count: number; deleted_ids: string[]; skipped_active_ids: string[] }> => {
    const response = await apiClient.post('/projects/history/delete', { path, force });
    return response.data;
  },

  deleteProject: async (path: string): Promise<{ project_path: string; deleted_count: number; deleted_ids: string[]; skipped_active_ids: string[] }> => {
    const response = await apiClient.delete(`/projects/${encodeURIComponent(path)}`);
    return response.data;
  },
};
