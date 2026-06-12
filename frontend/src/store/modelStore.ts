import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import type { ConfigData, ConfigUpdateRequest } from '../types/model';
import { modelApi } from '../api/model';
import { configApi } from '../api/config';

interface ModelState {
  providers: string[];
  models: Record<string, string[]>;
  config: ConfigData | null;
  currentProvider: string | null;
  currentModel: string | null;
  loading: boolean;
  error: string | null;
}

interface ModelActions {
  loadProviders: () => Promise<void>;
  loadModels: (provider: string) => Promise<void>;
  loadConfig: () => Promise<void>;
  updateConfig: (config: ConfigUpdateRequest) => Promise<void>;
  setCurrentProvider: (provider: string) => void;
  setCurrentModel: (model: string) => void;
  clearError: () => void;
}

export const useModelStore = create<ModelState & ModelActions>()(
  devtools((set, get) => ({
    providers: [],
    models: {} as Record<string, string[]>,
    config: null,
    currentProvider: null,
    currentModel: null,
    loading: false,
    error: null,

    loadProviders: async () => {
      set({ loading: true, error: null });
      try {
        const providerList = await modelApi.getProviders();
        set({ providers: providerList });
        // 不再在这里设置 currentProvider，由 loadConfig 负责
      } catch (err: any) {
        set({ error: err.message });
      } finally {
        set({ loading: false });
      }
    },

    loadModels: async (provider: string) => {
      set({ loading: true, error: null });
      try {
        const modelList = await modelApi.list(provider);
        set((state) => {
          // 只在 currentModel 为空或不在任何提供商中时才自动选择
          const currentModel = state.currentModel;
          const modelIsValid = currentModel && modelList.includes(currentModel);
          return {
            models: { ...state.models, [provider]: modelList },
            currentModel: modelIsValid ? currentModel : modelList[0] || null,
          };
        });
      } catch (err: any) {
        set({ error: err.message });
      } finally {
        set({ loading: false });
      }
    },

    loadConfig: async () => {
      set({ loading: true, error: null });
      try {
        const config = await configApi.get();
        set((state) => ({
          config,
          // 只在首次加载（currentProvider 为空）时设置默认提供商
          currentProvider: state.currentProvider || config.default_provider || null,
        }));
      } catch (err: any) {
        set({ error: err.message });
      } finally {
        set({ loading: false });
      }
    },

    updateConfig: async (configUpdate) => {
      set({ loading: true, error: null });
      try {
        await configApi.update(configUpdate);
        await get().loadConfig();
      } catch (err: any) {
        set({ error: err.message });
      } finally {
        set({ loading: false });
      }
    },

    setCurrentProvider: (provider) => {
      set({ currentProvider: provider });
    },

    setCurrentModel: (model) => {
      set({ currentModel: model });
    },

    clearError: () => set({ error: null }),
  }))
);
