import { apiClient } from './client';
import { leaseGuardedFetch } from './leaseFetch';
import { requireSuccessfulResponse } from './errors';
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

export interface PruneSummaryRecord {
  id: string;
  type: 'prune_summary';
  parent_node_id: string;
  created_at: number;
  model_id?: string | null;
  provider_id?: string | null;
  user_instructions?: string | null;
  summary: string;
  covered_node_ids: string[];
  covered_direct_child_ids: string[];
  compact_node_ids: string[];
  truncated_node_ids: string[];
  coverage_notes: string[];
  branch_digests?: Array<Record<string, unknown>>;
  tokens_used?: number;
  status: 'completed' | 'failed';
}

export interface PruneSummaryResult {
  conversation_id: string;
  parent_node_id: string;
  summary_id: string;
  covered_node_count: number;
  covered_direct_child_count: number;
  covered_node_ids?: string[];
  covered_direct_child_ids?: string[];
  compact_node_ids: string[];
  truncated_node_ids: string[];
  coverage_notes: string[];
  summary_preview: string;
  summary: string;
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

  resolveProjectFolder: async (path: string): Promise<WorkspaceContext> => {
    const response = await apiClient.post('/projects/folders/resolve', { path });
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

  pruneSummary: async (
    conversationId: string,
    nodeId: string,
    data: { custom_instructions?: string | null; model_id?: string | null; provider_id?: string | null } = {},
  ): Promise<PruneSummaryResult> => {
    const response = await apiClient.post(`/conversations/${conversationId}/nodes/${nodeId}/prune-summary`, data);
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

  fetchImportBlob: async (
    conversationId: string,
    filename: string,
    signal?: AbortSignal,
  ): Promise<Blob> => {
    const response = await leaseGuardedFetch(
      `/conversations/${conversationId}/imports/${encodeURIComponent(filename)}`,
      { signal },
    );
    await requireSuccessfulResponse(response);
    return response.blob();
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
