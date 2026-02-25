import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import type { ModelProvider, ConfigData, ConfigUpdateRequest } from '../types/model';
import { modelApi } from '../api/model';
import { configApi } from '../api/config';

interface ModelState {
  providers: ModelProvider[];
  models: Record<ModelProvider, string[]>;
  config: ConfigData | null;
  currentProvider: ModelProvider | null;
  currentModel: string | null;
  loading: boolean;
  error: string | null;
}

interface ModelActions {
  loadProviders: () => Promise<void>;
  loadModels: (provider: ModelProvider) => Promise<void>;
  loadConfig: () => Promise<void>;
  updateConfig: (config: ConfigUpdateRequest) => Promise<void>;
  setCurrentProvider: (provider: ModelProvider) => void;
  setCurrentModel: (model: string) => void;
  clearError: () => void;
}

export const useModelStore = create<ModelState & ModelActions>()(
  devtools((set, get) => ({
    providers: [],
    models: {} as Record<ModelProvider, string[]>,
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
        if (providerList.length > 0 && !get().currentProvider) {
          set({ currentProvider: providerList[0] });
        }
      } catch (err: any) {
        set({ error: err.message });
      } finally {
        set({ loading: false });
      }
    },

    loadModels: async (provider) => {
      set({ loading: true, error: null });
      try {
        const modelList = await modelApi.list(provider);
        set((state) => ({
          models: { ...state.models, [provider]: modelList },
          currentModel: modelList.includes(state.currentModel || '')
            ? state.currentModel
            : modelList[0] || null,
        }));
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
        set({
          config,
          currentProvider: config.default_provider,
        });
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