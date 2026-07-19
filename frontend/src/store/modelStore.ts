import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import type { ConfigData, ConfigUpdateRequest, ModelMetadata } from '../types/model';
import { modelApi } from '../api/model';
import { configApi } from '../api/config';

interface ModelState {
  providers: string[];
  models: Record<string, string[]>;
  /** 每个 provider 的模型元数据缓存：provider -> (model -> 元数据) */
  modelMetadata: Record<string, Record<string, ModelMetadata>>;
  config: ConfigData | null;
  /** 当前对话已确认的提供商 */
  currentProvider: string | null;
  /** 当前对话已确认的模型 */
  currentModel: string | null;
  /** 当前对话已确认的推理强度（null=不发送） */
  currentReasoningEffort: string | null;
  /** 当前对话已确认的思考开关（null=不发送） */
  currentThinkingEnabled: boolean | null;
  /** 对话框中临时选择的提供商（未确认） */
  pendingProvider: string | null;
  /** 对话框中临时选择的模型（未确认） */
  pendingModel: string | null;
  /** 对话框中临时选择的推理强度（未确认） */
  pendingReasoningEffort: string | null;
  /** 对话框中临时选择的思考开关（未确认） */
  pendingThinkingEnabled: boolean | null;
  loading: boolean;
  error: string | null;
}

interface ModelActions {
  loadProviders: () => Promise<void>;
  loadModels: (provider: string) => Promise<void>;
  /** 加载指定 provider 的模型元数据（已缓存则跳过） */
  loadMetadata: (provider: string) => Promise<void>;
  loadConfig: (options?: { force?: boolean }) => Promise<void>;
  updateConfig: (config: ConfigUpdateRequest) => Promise<void>;
  /** 设置临时提供商（对话框内预览） */
  setPendingProvider: (provider: string) => void;
  /** 设置临时模型（对话框内预览） */
  setPendingModel: (model: string) => void;
  /** 设置临时推理强度 */
  setPendingReasoningEffort: (effort: string | null) => void;
  /** 设置临时思考开关 */
  setPendingThinkingEnabled: (enabled: boolean | null) => void;
  /** 将 pending 选择确认为当前值，返回完整选择 */
  confirmModelSelection: () => {
    provider: string;
    model: string;
    reasoningEffort: string | null;
    thinkingEnabled: boolean | null;
  } | null;
  /** 取消 pending 选择，恢复到 current 值 */
  cancelModelSelection: () => void;
  /** 读取某 provider/model 的元数据（可能为 undefined） */
  getMetadata: (provider: string | null, model: string | null) => ModelMetadata | undefined;
  /**
   * 从会话元数据同步当前模型与推理设置（切换会话时调用）。
   * 若 providerId / modelId 为空，回退到默认提供商的默认模型。
   */
  syncFromConversation: (
    providerId: string | null | undefined,
    modelId: string | null | undefined,
    reasoningEffort?: string | null,
    thinkingEnabled?: boolean | null,
  ) => Promise<void>;
  /** 重置为默认提供商的默认模型（新建对话时调用） */
  resetToDefault: () => Promise<void>;
  clearError: () => void;
}

/** 按模型元数据推导默认推理设置（无控件时为 null）。 */
function defaultsFromMeta(meta: ModelMetadata | undefined): {
  effort: string | null;
  thinking: boolean | null;
} {
  const effortSpec = meta?.reasoning_effort;
  const thinkingSpec = meta?.thinking;
  return {
    effort: effortSpec?.default ?? null,
    thinking: thinkingSpec?.toggleable ? (thinkingSpec.default_enabled ?? false) : null,
  };
}

export function selectVisibleDefaultModel(
  configuredDefaultModel: string | null | undefined,
  visibleModels: string[],
): string | null {
  const candidate = configuredDefaultModel || '';
  if (candidate && visibleModels.includes(candidate)) {
    return candidate;
  }
  return visibleModels[0] || null;
}

const CONFIG_REFRESH_TTL_MS = 30_000;
let configLoadPromise: Promise<void> | null = null;
let lastConfigLoadedAt = 0;
let configLoadGeneration = 0;

export const useModelStore = create<ModelState & ModelActions>()(
  devtools((set, get) => ({
    providers: [],
    models: {} as Record<string, string[]>,
    modelMetadata: {} as Record<string, Record<string, ModelMetadata>>,
    config: null,
    currentProvider: null,
    currentModel: null,
    currentReasoningEffort: null,
    currentThinkingEnabled: null,
    pendingProvider: null,
    pendingModel: null,
    pendingReasoningEffort: null,
    pendingThinkingEnabled: null,
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
      // 聊天侧模型列表使用「设置」里已策划的配置（config.provider[provider].models），
      // 不做实时 /v1/models 拉取。否则聚合型网关（如 ustc）的 /v1/models 会返回整张
      // 目录（含 claude-* 等其它供应商的模型名），覆盖用户策划的列表，造成串台。
      // 实时拉取仅保留在「设置」页的「从 API 获取」按钮（直接调用 modelApi.list）。
      const cfg = get().config;
      const configured = cfg?.provider?.[provider]?.models || [];
      set((state) => ({
        models: { ...state.models, [provider]: configured },
      }));
    },

    loadMetadata: async (provider: string) => {
      if (get().modelMetadata[provider]) return; // 已缓存
      try {
        const meta = await modelApi.metadata(provider);
        set((state) => ({
          modelMetadata: { ...state.modelMetadata, [provider]: meta },
        }));
      } catch (err: any) {
        set({ error: err.message });
      }
    },

    getMetadata: (provider, model) => {
      if (!provider || !model) return undefined;
      return get().modelMetadata[provider]?.[model];
    },

    loadConfig: async (options = {}) => {
      const force = options.force === true;
      const now = Date.now();

      if (!force && configLoadPromise) {
        return configLoadPromise;
      }

      if (!force && get().config && now - lastConfigLoadedAt < CONFIG_REFRESH_TTL_MS) {
        return;
      }

      const generation = ++configLoadGeneration;
      set({ loading: true, error: null });

      let promise!: Promise<void>;
      promise = (async () => {
        try {
          const config = await configApi.get();
          if (generation !== configLoadGeneration) return;

          lastConfigLoadedAt = Date.now();
          const needInit = !get().currentProvider;
          set({ config });
          // 首次加载时初始化默认模型
          if (needInit) {
            await get().resetToDefault();
          }
        } catch (err: any) {
          if (generation === configLoadGeneration) {
            set({ error: err.message });
          }
        } finally {
          if (configLoadPromise === promise) {
            configLoadPromise = null;
            set({ loading: false });
          }
        }
      })();

      configLoadPromise = promise;
      return promise;
    },

    updateConfig: async (configUpdate) => {
      set({ loading: true, error: null });
      try {
        await configApi.update(configUpdate);
        await get().loadConfig({ force: true });
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

    setPendingReasoningEffort: (effort) => {
      set({ pendingReasoningEffort: effort });
    },

    setPendingThinkingEnabled: (enabled) => {
      set({ pendingThinkingEnabled: enabled });
    },

    confirmModelSelection: () => {
      const { pendingProvider, pendingModel, pendingReasoningEffort, pendingThinkingEnabled } = get();
      if (!pendingProvider || !pendingModel) return null;
      set({
        currentProvider: pendingProvider,
        currentModel: pendingModel,
        currentReasoningEffort: pendingReasoningEffort,
        currentThinkingEnabled: pendingThinkingEnabled,
        pendingProvider: null,
        pendingModel: null,
        pendingReasoningEffort: null,
        pendingThinkingEnabled: null,
      });
      return {
        provider: pendingProvider,
        model: pendingModel,
        reasoningEffort: pendingReasoningEffort,
        thinkingEnabled: pendingThinkingEnabled,
      };
    },

    cancelModelSelection: () => {
      set({
        pendingProvider: null,
        pendingModel: null,
        pendingReasoningEffort: null,
        pendingThinkingEnabled: null,
      });
    },

    syncFromConversation: async (providerId, modelId, reasoningEffort, thinkingEnabled) => {
      const { config, models: modelsMap, loadModels: loadM, loadMetadata: loadMeta } = get();
      if (!config) return;

      const defaultProvider = config.default_provider || null;
      const pId = providerId || defaultProvider;

      // 如果有 provider，确保其模型列表与元数据已加载
      if (pId && !modelsMap[pId]) {
        await loadM(pId);
      }
      if (pId) {
        await loadMeta(pId);
      }

      // 重新读取（loadModels 可能已更新 store）
      const updatedModels = get().models;
      const providerModels = pId ? (updatedModels[pId] || []) : [];
      const hiddenModels = pId ? (config.provider?.[pId]?.hidden_models || []) : [];
      const visibleModels = providerModels.filter(m => !hiddenModels.includes(m));

      // 确定模型：会话保存值 > 全局 default_model > 第一个可见模型。
      const mId = modelId && visibleModels.includes(modelId)
        ? modelId
        : selectVisibleDefaultModel(config.default_model, visibleModels);

      // 推理设置：对话保存值优先；缺省回退到所选模型元数据的默认。
      const meta = get().getMetadata(pId, mId);
      const defs = defaultsFromMeta(meta);
      set({
        currentProvider: pId,
        currentModel: mId,
        currentReasoningEffort: reasoningEffort !== undefined && reasoningEffort !== null ? reasoningEffort : defs.effort,
        currentThinkingEnabled: thinkingEnabled !== undefined && thinkingEnabled !== null ? thinkingEnabled : defs.thinking,
      });
    },

    resetToDefault: async () => {
      const { config, loadModels: loadM, loadMetadata: loadMeta } = get();
      if (!config) return;

      const pId = config.default_provider || null;
      if (pId) {
        const modelsMap = get().models;
        if (!modelsMap[pId]) {
          await loadM(pId);
        }
        await loadMeta(pId);
        const updatedModels = get().models;
        const providerModels = updatedModels[pId] || [];
        const hiddenModels = config.provider?.[pId]?.hidden_models || [];
        const visibleModels = providerModels.filter(m => !hiddenModels.includes(m));
        const mId = selectVisibleDefaultModel(config.default_model, visibleModels);
        const defs = defaultsFromMeta(get().getMetadata(pId, mId));
        set({
          currentProvider: pId,
          currentModel: mId,
          currentReasoningEffort: defs.effort,
          currentThinkingEnabled: defs.thinking,
        });
      } else {
        set({
          currentProvider: null,
          currentModel: null,
          currentReasoningEffort: null,
          currentThinkingEnabled: null,
        });
      }
    },

    clearError: () => set({ error: null }),
  }))
);
