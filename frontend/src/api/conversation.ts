import { apiClient } from './client';
import type { Conversation, ConversationCreateRequest, MultiAgentMode, WorkspaceContext } from '../types/conversation';
import type { NodeUsage, UsageInfo } from '../types/message';

export interface TreeNode {
  id: string;
  parent_id: string | null;
  children_ids: string[];
  user_content: string;
  user_subtype?: string | null;
  assistant_content: string;
  model_id: string | null;
  task_context_mode: 'attached' | 'detached';
  timestamp: number;
  is_current: boolean;
  is_root: boolean;
  total_tokens?: number;
  branch_usage_info?: UsageInfo | null;
  usage?: NodeUsage | null;
}

export interface TreeData {
  root_node_id: string;
  current_node_id: string;
  nodes: TreeNode[];
}

export interface DeleteNodeOptions {
  force?: boolean;
}

export const conversationApi = {
  // ��ȡ�Ի��б�
  list: async (): Promise<Conversation[]> => {
    const response = await apiClient.get('/conversations');
    return response.data;
  },

  // �����Ի�
  create: async (data: ConversationCreateRequest = {}): Promise<Conversation> => {
    const response = await apiClient.post('/conversations', data);
    return response.data;
  },

  createProjectFolder: async (path: string, label?: string): Promise<WorkspaceContext> => {
    const response = await apiClient.post('/projects/folders', { path, label: label || null });
    return response.data;
  },

  resolveProjectFolder: async (path: string, label?: string): Promise<WorkspaceContext> => {
    const response = await apiClient.post('/projects/folders/resolve', { path, label: label || null });
    return response.data;
  },

  // ɾ���Ի�
  delete: async (id: string): Promise<void> => {
    await apiClient.delete(`/conversations/${id}`);
  },

  // �л��ڵ�
  switchNode: async (conversationId: string, nodeId: string): Promise<void> => {
    await apiClient.post(`/conversations/${conversationId}/switch/${nodeId}`);
  },

  // ��ȡ��֧
  getBranches: async (conversationId: string): Promise<any> => {
    const response = await apiClient.get(`/conversations/${conversationId}/branches`);
    return response.data;
  },

  // ��ȡ�������ṹ
  getTree: async (conversationId: string): Promise<TreeData> => {
    const response = await apiClient.get(`/conversations/${conversationId}/tree`);
    return response.data;
  },

  // ���¶Ի�����
  updateTitle: async (id: string, title: string): Promise<void> => {
    await apiClient.patch(`/conversations/${id}`, { title });
  },

  updateModel: async (
    id: string,
    modelId: string,
    providerId: string,
    reasoningEffort?: string | null,
    thinkingEnabled?: boolean | null,
  ): Promise<void> => {
    await apiClient.patch(`/conversations/${id}/model`, {
      model_id: modelId,
      provider_id: providerId,
      reasoning_effort: reasoningEffort ?? null,
      thinking_enabled: thinkingEnabled ?? null,
    });
  },

  updateMultiAgentMode: async (
    id: string,
    multiAgentMode: MultiAgentMode,
  ): Promise<void> => {
    await apiClient.patch(`/conversations/${id}/multi-agent-mode`, {
      multi_agent_mode: multiAgentMode,
    });
  },

  compact: async (
    id: string,
    data: { custom_instructions?: string | null; model_id?: string | null; provider_id?: string | null; messages_to_keep?: number | null } = {},
  ): Promise<{ conversation_id: string; node_id: string; pre_tokens?: number; tokens_used?: number; trigger?: string }> => {
    const response = await apiClient.post(`/conversations/${id}/compact`, data);
    return response.data;
  },

  // ɾ���ڵ�
  // �ϴ������ļ�
  uploadImport: async (
    conversationId: string,
    file: File,
  ): Promise<{ filename: string; size: number; kind?: 'file' | 'image'; mime_type?: string | null }> => {
    const formData = new FormData();
    formData.append('file', file);
    const response = await apiClient.post(`/conversations/${conversationId}/imports`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
    return response.data;
  },

  // �г������ļ�
  listImports: async (conversationId: string): Promise<Array<{ filename: string; size: number }>> => {
    const response = await apiClient.get(`/conversations/${conversationId}/imports`);
    return response.data;
  },

  // ɾ�������ļ�
  deleteImport: async (conversationId: string, filename: string): Promise<void> => {
    await apiClient.delete(`/conversations/${conversationId}/imports/${encodeURIComponent(filename)}`);
  },

  deleteNode: async (conversationId: string, nodeId: string, options: DeleteNodeOptions = {}): Promise<{ deleted_node_id: string; new_current_node_id: string; parent_node_id: string }> => {
    const response = await apiClient.delete(`/conversations/${conversationId}/nodes/${nodeId}`, {
      params: options.force ? { force: true } : undefined,
    });
    return response.data;
  },
};
