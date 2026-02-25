import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import type { Prompt, PromptResponse } from '../types/prompt';
import { promptApi } from '../api/prompt';

interface PromptState {
  prompts: PromptResponse[];         // 提示词列表（轻量数据）
  currentPrompt: Prompt | null;      // 当前选中的完整提示词
  loading: boolean;
  error: string | null;
}

interface PromptActions {
  loadPrompts: () => Promise<void>;  // 加载列表
  loadPrompt: (id: string) => Promise<void>; // 加载单个详情
  savePrompt: (data: Prompt) => Promise<void>; // 保存提示词
  deletePrompt: (id: string) => Promise<void>; // 删除提示词
  clearCurrentPrompt: () => void; // 清除当前选中的提示词
  clearError: () => void;
  reset: () => void;
}

const usePromptStoreBase = create<PromptState & PromptActions>()(
  devtools(
    (set, get) => ({
      // 初始状态
      prompts: [],
      currentPrompt: null,
      loading: false,
      error: null,

      // 加载提示词列表
      loadPrompts: async () => {
        set({ loading: true, error: null });
        try {
          const response = await promptApi.list();
          set({ prompts: response.prompts });
        } catch (err: any) {
          set({ error: err.message });
        } finally {
          set({ loading: false });
        }
      },

      // 加载单个提示词详情
      loadPrompt: async (id: string) => {
        set({ loading: true, error: null });
        try {
          const prompt = await promptApi.load(id);
          set({ currentPrompt: prompt });
        } catch (err: any) {
          set({ error: err.message });
        } finally {
          set({ loading: false });
        }
      },

      // 保存提示词
      savePrompt: async (data: Prompt) => {
        set({ loading: true, error: null });
        try {
          await promptApi.save(data);
          // 保存成功后刷新列表
          await get().loadPrompts();
        } catch (err: any) {
          set({ error: err.message });
        } finally {
          set({ loading: false });
        }
      },

      // 删除提示词
      deletePrompt: async (id: string) => {
        set({ loading: true, error: null });
        try {
          await promptApi.delete(id);
          // 如果删除的是当前选中的提示词，清除选中状态
          if (get().currentPrompt?.id === id) {
            set({ currentPrompt: null });
          }
          // 删除成功后刷新列表
          await get().loadPrompts();
        } catch (err: any) {
          set({ error: err.message });
          throw err;
        } finally {
          set({ loading: false });
        }
      },

      // 清除错误
      clearError: () => set({ error: null }),

      // 清除当前选中的提示词
      clearCurrentPrompt: () => set({ currentPrompt: null }),

      // 重置所有状态
      reset: () => set({ 
        prompts: [], 
        currentPrompt: null, 
        loading: false, 
        error: null 
      }),
    }),
    { name: 'prompt-store' } // 调试工具名称
  )
);

export const usePromptStore = () => usePromptStoreBase();