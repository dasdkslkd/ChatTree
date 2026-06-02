import { apiClient } from './client';
import type { Conversation, ConversationCreateRequest } from '../types/conversation';

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

  // 更新对话标题
  updateTitle: async (id: string, title: string): Promise<void> => {
    await apiClient.patch(`/conversations/${id}`, { title });
  },

  // 删除节点
  deleteNode: async (conversationId: string, nodeId: string): Promise<{ deleted_node_id: string; new_current_node_id: string; parent_node_id: string }> => {
    const response = await apiClient.delete(`/conversations/${conversationId}/nodes/${nodeId}`);
    return response.data;
  },
};