import { apiClient } from './client';
import type { PromptResponse, Prompt, ListPromptsResponse } from '../types/prompt';

export const promptApi = {
    // 列出所有提示词
    list: async (): Promise<ListPromptsResponse> => {
        const response = await apiClient.get('/prompts');
        return response.data;
    },

    // 保存提示词
    save: async (data: Prompt): Promise<PromptResponse> => {
        const response = await apiClient.post('/prompts', data);
        return response.data;
    },
  
    // 加载单个提示词
    load: async (id: string): Promise<Prompt> => {
        const response = await apiClient.get(`/prompts/${id}`);
        return response.data;
    },

    // 删除提示词
    delete: async (id: string): Promise<void> => {
        await apiClient.delete(`/prompts/${id}`);
    },
}