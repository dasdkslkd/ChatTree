import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import type { ConfigData, ConfigUpdateRequest } from '../types/model';
import { modelApi } from '../api/model';
import { configApi } from '../api/config';

interface ModelState {
  providers: string[];
  models: Record<string, string[]>;
  config: ConfigData | null;
  /** 当前对话已确认的提供商 */
  currentProvider: string | null;
  /** 当前对话已确认的模型 */
  currentModel: string | null;
  /** 对话框中临时选择的提供商（未确认） */
  pendingProvider: string | null;
  /** 对话框中临时选择的模型（未确认） */
  pendingModel: string | null;
  loading: boolean;
  error: string | null;
}

interface ModelActions {
  loadProviders: () => Promise<void>;
  loadModels: (provider: string) => Promise<void>;
  loadConfig: () => Promise<void>;
  updateConfig: (config: ConfigUpdateRequest) => Promise<void>;
  /** 设置临时提供商（对话框内预览） */
  setPendingProvider: (provider: string) => void;
  /** 设置临时模型（对话框内预览） */
  setPendingModel: (model: string) => void;
  /** 将 pending 选择确认为当前值，返回 {provider, model} */
  confirmModelSelection: () => { provider: string; model: string } | null;
  /** 取消 pending 选择，恢复到 current 值 */
  cancelModelSelection: () => void;
  /**
   * 从会话元数据同步当前模型（切换会话时调用）。
   * 若 providerId / modelId 为空，回退到默认提供商的默认模型。
   */
  syncFromConversation: (providerId: string | null | undefined, modelId: string | null | undefined) => Promise<void>;
  /** 重置为默认提供商的默认模型（新建对话时调用） */
  resetToDefault: () => Promise<void>;
  clearError: () => void;
}

export const useModelStore = create<ModelState & ModelActions>()(
  devtools((set, get) => ({
    providers: [],
    models: {} as Record<string, string[]>,
    config: null,
    currentProvider: null,
    currentModel: null,
    pendingProvider: null,
    pendingModel: null,
    loading: false,
    error: null,

    loadProviders: async () => {
      set({ loading: true, error: null });
      try {
        const providerList = await modelApi.getProviders();
        set({ providers: providerList });
      } catch (err: any) {
        set({ error: err.message });
      } finally {
        set({ loading: false });
      }
    },

    loadModels: async (provider: string) => {
      try {
        const modelList = await modelApi.list(provider);
        set((state) => ({
          models: { ...state.models, [provider]: modelList },
        }));
        return;
      } catch (err: any) {
        set({ error: err.message });
      }
    },

    loadConfig: async () => {
      set({ loading: true, error: null });
      try {
        const config = await configApi.get();
        const needInit = !get().currentProvider;
        set({ config });
        // 首次加载时初始化默认模型
        if (needInit) {
          await get().resetToDefault();
        }
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

    setPendingProvider: (provider) => {
      set({ pendingProvider: provider });
    },

    setPendingModel: (model) => {
      set({ pendingModel: model });
    },

    confirmModelSelection: () => {
      const { pendingProvider, pendingModel } = get();
      if (!pendingProvider || !pendingModel) return null;
      set({
        currentProvider: pendingProvider,
        currentModel: pendingModel,
        pendingProvider: null,
        pendingModel: null,
      });
      return { provider: pendingProvider, model: pendingModel };
    },

    cancelModelSelection: () => {
      set({ pendingProvider: null, pendingModel: null });
    },

    syncFromConversation: async (providerId, modelId) => {
      const { config, models: modelsMap, loadModels: loadM } = get();
      if (!config) return;

      const defaultProvider = config.default_provider || null;
      const pId = providerId || defaultProvider;

      // 如果有 provider，确保其模型列表已加载
      if (pId && !modelsMap[pId]) {
        await loadM(pId);
      }

      // 重新读取（loadModels 可能已更新 store）
      const updatedModels = get().models;
      const providerModels = pId ? (updatedModels[pId] || []) : [];
      const hiddenModels = pId ? (config.provider?.[pId]?.hidden_models || []) : [];
      const visibleModels = providerModels.filter(m => !hiddenModels.includes(m));

      // 确定模型：metadata 中的 > provider 的 default_model > 第一个可见模型
      const mId = modelId && visibleModels.includes(modelId)
        ? modelId
        : (pId && config.provider?.[pId]?.default_model && visibleModels.includes(config.provider[pId].default_model)
          ? config.provider[pId].default_model
          : visibleModels[0] || null);

      set({ currentProvider: pId, currentModel: mId });
    },

    resetToDefault: async () => {
      const { config, loadModels: loadM } = get();
      if (!config) return;

      const pId = config.default_provider || null;
      if (pId) {
        const modelsMap = get().models;
        if (!modelsMap[pId]) {
          await loadM(pId);
        }
        const updatedModels = get().models;
        const providerModels = updatedModels[pId] || [];
        const hiddenModels = config.provider?.[pId]?.hidden_models || [];
        const visibleModels = providerModels.filter(m => !hiddenModels.includes(m));
        const defaultModel = config.provider?.[pId]?.default_model;
        const mId = defaultModel && visibleModels.includes(defaultModel)
          ? defaultModel
          : visibleModels[0] || null;
        set({ currentProvider: pId, currentModel: mId });
      } else {
        set({ currentProvider: null, currentModel: null });
      }
    },

    clearError: () => set({ error: null }),
  }))
);
