import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import type { Prompt, PromptResponse } from '../types/prompt';
import { promptApi } from '../api/prompt';
import {
  StaleConnectionEpochError,
  captureConnectionEpoch,
  commitForConnectionEpoch as commitForConnectionEpochStrict,
  connectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';

interface PromptState {
  prompts: PromptResponse[];         // 提示词列表（轻量数据）
  currentPrompt: Prompt | null;      // 当前选中的完整提示词
  loading: boolean;
  error: string | null;
}

interface PromptActions {
  loadPrompts: (token?: ConnectionEpochToken) => Promise<void>;  // 加载列表
  loadPrompt: (id: string, token?: ConnectionEpochToken) => Promise<void>; // 加载单个详情
  savePrompt: (data: Prompt, token?: ConnectionEpochToken) => Promise<void>; // 保存提示词
  deletePrompt: (id: string, token?: ConnectionEpochToken) => Promise<void>; // 删除提示词
  clearCurrentPrompt: () => void; // 清除当前选中的提示词
  clearError: () => void;
  reset: () => void;
}

function resolveEpochToken(token?: ConnectionEpochToken): ConnectionEpochToken {
  if (token) {
    connectionEpochRuntime.assertCurrent(token);
    return token;
  }
  return captureConnectionEpoch();
}

function isStaleEpoch(error: unknown, token: ConnectionEpochToken | null): boolean {
  return !token
    || error instanceof StaleConnectionEpochError
    || !connectionEpochRuntime.isCurrent(token);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function commitForConnectionEpoch(
  token: ConnectionEpochToken | null,
  commit: () => void,
): boolean {
  return token ? commitForConnectionEpochStrict(token, commit) : false;
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
      loadPrompts: async (ownerToken) => {
        let token: ConnectionEpochToken | null = null;
        try {
          token = resolveEpochToken(ownerToken);
          set({ loading: true, error: null });
          const response = await promptApi.list();
          commitForConnectionEpoch(token, () => set({ prompts: response.prompts }));
        } catch (err: unknown) {
          if (isStaleEpoch(err, token)) return;
          commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
        } finally {
          if (token) commitForConnectionEpoch(token, () => set({ loading: false }));
        }
      },

      // 加载单个提示词详情
      loadPrompt: async (id: string, ownerToken?: ConnectionEpochToken) => {
        let token: ConnectionEpochToken | null = null;
        try {
          token = resolveEpochToken(ownerToken);
          set({ loading: true, error: null });
          const prompt = await promptApi.load(id);
          commitForConnectionEpoch(token, () => set({ currentPrompt: prompt }));
        } catch (err: unknown) {
          if (isStaleEpoch(err, token)) return;
          commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
        } finally {
          if (token) commitForConnectionEpoch(token, () => set({ loading: false }));
        }
      },

      // 保存提示词
      savePrompt: async (data: Prompt, ownerToken?: ConnectionEpochToken) => {
        let token: ConnectionEpochToken | null = null;
        try {
          token = resolveEpochToken(ownerToken);
          set({ loading: true, error: null });
          await promptApi.save(data);
          connectionEpochRuntime.assertCurrent(token);
          // 保存成功后刷新列表
          await get().loadPrompts(token);
          connectionEpochRuntime.assertCurrent(token);
        } catch (err: unknown) {
          if (isStaleEpoch(err, token)) return;
          commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
        } finally {
          if (token) commitForConnectionEpoch(token, () => set({ loading: false }));
        }
      },

      // 删除提示词
      deletePrompt: async (id: string, ownerToken?: ConnectionEpochToken) => {
        let token: ConnectionEpochToken | null = null;
        try {
          token = resolveEpochToken(ownerToken);
          set({ loading: true, error: null });
          await promptApi.delete(id);
          connectionEpochRuntime.assertCurrent(token);
          // 如果删除的是当前选中的提示词，清除选中状态
          if (get().currentPrompt?.id === id) {
            commitForConnectionEpoch(token, () => set({ currentPrompt: null }));
          }
          // 删除成功后刷新列表
          await get().loadPrompts(token);
          connectionEpochRuntime.assertCurrent(token);
        } catch (err: unknown) {
          if (isStaleEpoch(err, token)) return;
          commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
          throw err;
        } finally {
          if (token) commitForConnectionEpoch(token, () => set({ loading: false }));
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
export const promptStore = usePromptStoreBase;
