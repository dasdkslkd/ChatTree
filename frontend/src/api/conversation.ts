import { apiClient } from './client';
import type { Conversation, ConversationCreateRequest, WorkspaceContext } from '../types/conversation';

export interface TreeNode {
  id: string;
  parent_id: string | null;
  children_ids: string[];
  user_content: string;
  assistant_content: string;
  model_id: string | null;
  timestamp: number;
  is_current: boolean;
  is_root: boolean;
}

export interface TreeData {
  root_node_id: string;
  current_node_id: string;
  nodes: TreeNode[];
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

  // ɾ���ڵ�
  // �ϴ������ļ�
  uploadImport: async (conversationId: string, file: File): Promise<{ filename: string; size: number }> => {
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

  deleteNode: async (conversationId: string, nodeId: string): Promise<{ deleted_node_id: string; new_current_node_id: string; parent_node_id: string }> => {
    const response = await apiClient.delete(`/conversations/${conversationId}/nodes/${nodeId}`);
    return response.data;
  },
};
