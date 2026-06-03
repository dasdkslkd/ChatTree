import { apiClient } from './client';
import type { Conversation, ConversationCreateRequest } from '../types/conversation';

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
  // 获取对话列表
  list: async (): Promise<Conversation[]> => {
    const response = await apiClient.get('/conversations');
    return response.data;
  },

  // 创建对话
  create: async (data: ConversationCreateRequest = {}): Promise<Conversation> => {
    const response = await apiClient.post('/conversations', data);
    return response.data;
  },

  // 删除对话
  delete: async (id: string): Promise<void> => {
    await apiClient.delete(`/conversations/${id}`);
  },

  // 切换节点
  switchNode: async (conversationId: string, nodeId: string): Promise<void> => {
    await apiClient.post(`/conversations/${conversationId}/switch/${nodeId}`);
  },

  // 获取分支
  getBranches: async (conversationId: string): Promise<any> => {
    const response = await apiClient.get(`/conversations/${conversationId}/branches`);
    return response.data;
  },

  // 获取完整树结构
  getTree: async (conversationId: string): Promise<TreeData> => {
    const response = await apiClient.get(`/conversations/${conversationId}/tree`);
    return response.data;
  },

  // 更新对话标题
  updateTitle: async (id: string, title: string): Promise<void> => {
    await apiClient.patch(`/conversations/${id}`, { title });
  },

  // 删除节点
  // 上传导入文件
  uploadImport: async (conversationId: string, file: File): Promise<{ filename: string; size: number }> => {
    const formData = new FormData();
    formData.append('file', file);
    const response = await apiClient.post(`/conversations/${conversationId}/imports`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
    return response.data;
  },

  // 列出导入文件
  listImports: async (conversationId: string): Promise<Array<{ filename: string; size: number }>> => {
    const response = await apiClient.get(`/conversations/${conversationId}/imports`);
    return response.data;
  },

  // 删除导入文件
  deleteImport: async (conversationId: string, filename: string): Promise<void> => {
    await apiClient.delete(`/conversations/${conversationId}/imports/${encodeURIComponent(filename)}`);
  },

  deleteNode: async (conversationId: string, nodeId: string): Promise<{ deleted_node_id: string; new_current_node_id: string; parent_node_id: string }> => {
    const response = await apiClient.delete(`/conversations/${conversationId}/nodes/${nodeId}`);
    return response.data;
  },
};
