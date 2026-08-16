import { create } from 'zustand';
import { devtools, persist } from 'zustand/middleware';
import type {
  Conversation,
  ConversationCreateRequest,
  MultiAgentMode,
} from '../types/conversation';
import { conversationApi, type AvailableBranch, type TreeData } from '../api/conversation';
import { ChatTreeApiError } from '../api/errors';
import { transcriptService } from '../services/transcript';
import type { TranscriptItem } from '../types/transcript';
import { useModelStore } from './modelStore';
import { getProfileContext } from '../runtime/profileContext';
import {
  CONVERSATION_STORAGE_KEY,
  profileStorageKey,
} from '../runtime/profileStorage';

const conversationStorageKey = profileStorageKey(
  getProfileContext().profileId,
  CONVERSATION_STORAGE_KEY,
);

// loadTree 同 conversation 并发去重：in-flight Promise 共享，完成后清理（失败也清理以便重试）
const loadTreeInFlight = new Map<string, Promise<void>>();

interface ConversationState {
  conversations: Conversation[];
  currentConversation: Conversation | null;
  branches: AvailableBranch[];
  treeData: TreeData | null;
  currentNodeId: string | null;
  pendingScrollNodeId: string | null;
  loading: boolean;
}

interface ConversationActions {
  loadConversations: () => Promise<void>;
  createConversation: (request?: ConversationCreateRequest) => Promise<Conversation>;
  selectConversation: (id: string) => Promise<void>;
  deleteConversation: (id: string) => Promise<void>;
  updateConversationTitle: (id: string, title: string) => Promise<void>;
  updateConversationModel: (
    id: string,
    modelId: string,
    providerId: string,
    reasoningEffort?: string | null,
    thinkingEnabled?: boolean | null,
  ) => Promise<boolean>;
  updateMultiAgentMode: (id: string, mode: MultiAgentMode) => Promise<void>;
  clearCurrentConversation: () => void;
  switchNode: (nodeId: string) => Promise<void>;
  setCurrentNodeIdLocal: (nodeId: string) => void;
  deleteNode: (nodeId: string) => Promise<void>;
  loadTree: (conversationId: string) => Promise<void>;
  clearPendingScroll: () => void;
  refreshMessages: (conversationId: string, opts?: { awaitNodeId?: string; awaitRole?: 'assistant' | 'user'; retries?: number }) => Promise<boolean>;
  refreshBranches: (conversationId: string) => Promise<boolean>;
}

function isActiveRunDeleteConflict(err: unknown): boolean {
  return err instanceof ChatTreeApiError
    && err.status === 409
    && err.code === 'active_runs_present';
}

async function deleteNodeAllowingActiveRuns(conversationId: string, nodeId: string) {
  try {
    return await conversationApi.deleteNode(conversationId, nodeId);
  } catch (err) {
    if (!isActiveRunDeleteConflict(err)) throw err;
    return await conversationApi.deleteNode(conversationId, nodeId, { force: true });
  }
}

const useConversationStoreBase = create<ConversationState & ConversationActions>()(
  devtools(
    persist(
      (set, get) => ({
        conversations: [],
        currentConversation: null,
        branches: [],
        treeData: null,
        currentNodeId: null,
        pendingScrollNodeId: null,
        loading: false,

        loadConversations: async () => {
          set({ loading: true });
          try {
            const data = await conversationApi.list();
            set({ conversations: data });
          } catch {
            // 后台列表刷新失败静默，由轮询/重试兜底
          } finally {
            set({ loading: false });
          }
        },

        createConversation: async (request) => {
          set({ loading: true });
          try {
            const conversation = await conversationApi.create(request);
            // 本地插入列表头部（避免整表重拉）；下次 loadConversations 会与后端排序对齐
            set((state) => ({
              conversations: [conversation, ...state.conversations],
              currentConversation: conversation,
              treeData: null,
              currentNodeId: null,
              pendingScrollNodeId: null,
            }));
            // 保留用户已选模型：新建对话前若用户已在模型框里选过模型（store 里有
            // current 值），把它持久化到新对话，并保持 store 不变——否则
            // resetToDefault() 会把按钮显示回退到默认模型（请求仍用已选模型，但
            // 显示串掉）。仅当 store 没有任何选择时才回退默认作为初始化。
            const ms = useModelStore.getState();
            if (ms.currentProvider && ms.currentModel) {
              const updatedConversation = {
                ...conversation,
                model_id: ms.currentModel,
                provider_id: ms.currentProvider,
                reasoning_effort: ms.currentReasoningEffort,
                thinking_enabled: ms.currentThinkingEnabled,
              };
              void conversationApi.updateModel(
                conversation.id,
                ms.currentModel,
                ms.currentProvider,
                ms.currentReasoningEffort,
                ms.currentThinkingEnabled,
              ).then(() => {
                set((state) => ({
                  conversations: state.conversations.map((item) => (
                    item.id === conversation.id ? { ...item, ...updatedConversation } : item
                  )),
                  currentConversation: state.currentConversation?.id === conversation.id
                    ? { ...state.currentConversation, ...updatedConversation }
                    : state.currentConversation,
                }));
              }).catch(() => {
                // 持久化失败不阻断创建；store 选择仍保留，显示不会串
              });
            } else {
              void ms.resetToDefault();
            }
            return conversation;
          } finally {
            set({ loading: false });
          }
        },

        selectConversation: async (id) => {
          const conversation = get().conversations.find((c) => c.id === id);
          const currentNodeId = conversation?.current_node_id || null;
          // 本地会话立即切换（先切 UI、不卡 loading），branches 后台并行填充
          set({
            currentConversation: conversation
              ? { ...conversation, current_node_id: currentNodeId || conversation.current_node_id }
              : null,
            branches: [],
            treeData: null,
            currentNodeId,
            pendingScrollNodeId: null,
          });
          const [branches] = await Promise.all([
            conversationApi.getBranches(id),
            // 同步模型选择到 modelStore
            useModelStore.getState().syncFromConversation(
              conversation?.provider_id || null,
              conversation?.model_id || null,
            ),
          ]);
          // await 期间用户可能已切走，避免旧会话 branches 覆盖当前会话
          if (get().currentConversation?.id === id) {
            set({ branches });
          }
        },

        deleteConversation: async (id) => {
          const isCurrent = get().currentConversation?.id === id;
          await conversationApi.delete(id);
          set((state) => ({
            conversations: state.conversations.filter((c) => c.id !== id),
            currentConversation: isCurrent ? null : state.currentConversation,
            branches: isCurrent ? [] : state.branches,
            treeData: isCurrent ? null : state.treeData,
            currentNodeId: isCurrent ? null : state.currentNodeId,
            pendingScrollNodeId: isCurrent ? null : state.pendingScrollNodeId,
          }));
          if (isCurrent) {
            await useModelStore.getState().resetToDefault();
          }
        },

        updateConversationTitle: async (id, title) => {
          await conversationApi.updateTitle(id, title);
          set((state) => ({
            conversations: state.conversations.map((c) =>
              c.id === id ? { ...c, title } : c
            ),
            currentConversation:
              state.currentConversation?.id === id
                ? { ...state.currentConversation, title }
                : state.currentConversation,
          }));
        },

        updateConversationModel: async (
          id,
          modelId,
          providerId,
          reasoningEffort,
          thinkingEnabled,
        ) => {
          await conversationApi.updateModel(
            id,
            modelId,
            providerId,
            reasoningEffort,
            thinkingEnabled,
          );
          set((state) => ({
            conversations: state.conversations.map((conversation) => (
              conversation.id === id
                ? {
                  ...conversation,
                  model_id: modelId,
                  provider_id: providerId,
                  reasoning_effort: reasoningEffort,
                  thinking_enabled: thinkingEnabled,
                }
                : conversation
            )),
            currentConversation: state.currentConversation?.id === id
              ? {
                ...state.currentConversation,
                model_id: modelId,
                provider_id: providerId,
                reasoning_effort: reasoningEffort,
                thinking_enabled: thinkingEnabled,
              }
              : state.currentConversation,
          }));
          return true;
        },

        updateMultiAgentMode: async (id, mode) => {
          try {
            await conversationApi.updateMultiAgentMode(id, mode);
            set((state) => ({
              conversations: state.conversations.map((conversation) =>
                conversation.id === id ? { ...conversation, multi_agent_mode: mode } : conversation
              ),
              currentConversation:
                state.currentConversation?.id === id
                  ? { ...state.currentConversation, multi_agent_mode: mode }
                  : state.currentConversation,
            }));
          } catch {
            // 后台 Agent 模式同步失败静默，下次切换会重试
          }
        },

        switchNode: async (nodeId) => {
          const { currentConversation } = get();
          if (!currentConversation) return;

          set({ loading: true });
          try {
            await conversationApi.switchNode(currentConversation.id, nodeId);
            const [branches, treeData] = await Promise.all([
              conversationApi.getBranches(currentConversation.id),
              conversationApi.getTree(currentConversation.id),
            ]);

            set((state) => ({
              branches,
              treeData,
              currentNodeId: nodeId,
              currentConversation: state.currentConversation
                ? { ...state.currentConversation, current_node_id: nodeId }
                : state.currentConversation,
              conversations: state.conversations.map((conversation) =>
                conversation.id === currentConversation.id
                  ? { ...conversation, current_node_id: nodeId }
                  : conversation
              ),
              pendingScrollNodeId: nodeId,
            }));
          } finally {
            set({ loading: false });
          }
        },

        setCurrentNodeIdLocal: (nodeId) => {
          const currentConversationId = get().currentConversation?.id;
          set((state) => ({
            currentNodeId: nodeId,
            currentConversation: state.currentConversation
              ? { ...state.currentConversation, current_node_id: nodeId }
              : state.currentConversation,
            conversations: currentConversationId
              ? state.conversations.map((conversation) =>
                conversation.id === currentConversationId
                  ? { ...conversation, current_node_id: nodeId }
                  : conversation
              )
              : state.conversations,
          }));
        },

        clearPendingScroll: () => set({ pendingScrollNodeId: null }),

        // 流式结束后，用 canonical transcript 快照确认指定节点/角色已经落盘。
        // 不再读取旧 Message chain，也不在前端把 transcript 反转回 Message[]。
        refreshMessages: async (
          conversationId: string,
          opts?: { awaitNodeId?: string; awaitRole?: 'assistant' | 'user'; retries?: number },
        ): Promise<boolean> => {
          if (get().currentConversation?.id !== conversationId) return false;
          const awaitNodeId = opts?.awaitNodeId;
          const awaitRole = opts?.awaitRole ?? 'assistant';
          const retries = opts?.retries ?? 0;
          const landed = (items: TranscriptItem[]) =>
            !awaitNodeId || items.some((item) => (
              item.node_id === awaitNodeId
              && item.type === (awaitRole === 'assistant' ? 'assistant_answer' : 'user_message')
            ));
          for (let attempt = 0; attempt <= retries; attempt++) {
            try {
              const tipNodeId = get().currentNodeId || get().currentConversation?.current_node_id;
              if (!tipNodeId) return false;
              const snapshot = await transcriptService.fetchBranchSnapshot(conversationId, tipNodeId);
              // 再次校验：await 期间用户可能已切走
              if (get().currentConversation?.id !== conversationId) return false;
              const ok = landed(snapshot.items);
              if (ok || attempt === retries) {
                if (!ok && awaitNodeId) {
                  return false;
                }
                const currentNodeId = snapshot.node_id || get().currentNodeId;
                set((state) => ({
                  currentNodeId,
                  currentConversation: state.currentConversation?.id === conversationId
                    ? { ...state.currentConversation, current_node_id: currentNodeId || state.currentConversation.current_node_id }
                    : state.currentConversation,
                  conversations: state.conversations.map((conversation) =>
                    conversation.id === conversationId
                      ? { ...conversation, current_node_id: currentNodeId || conversation.current_node_id }
                      : conversation
                  ),
                }));
                return ok;
              }
              // 后端尚未保存完成，指数退避重试（保留乐观气泡）
              await new Promise((r) => setTimeout(r, 150 * 2 ** attempt));
            } catch {
              return false;
            }
          }
          return false;
        },

        refreshBranches: async (conversationId: string): Promise<boolean> => {
          if (get().currentConversation?.id !== conversationId) return false;
          try {
            const branches = await conversationApi.getBranches(conversationId);
            if (get().currentConversation?.id !== conversationId) return false;
            set({ branches });
            return true;
          } catch {
            return false;
          }
        },

        clearCurrentConversation: () => {
          set({
            currentConversation: null,
            branches: [],
            treeData: null,
            currentNodeId: null,
            pendingScrollNodeId: null,
          });
          // 清空对话时重置为默认模型
          useModelStore.getState().resetToDefault();
        },

        deleteNode: async (nodeId) => {
          const { currentConversation } = get();
          if (!currentConversation) return;
          set({ loading: true });
          try {
            const result = await deleteNodeAllowingActiveRuns(currentConversation.id, nodeId);
            const [branches, treeData] = await Promise.all([
              conversationApi.getBranches(currentConversation.id),
              conversationApi.getTree(currentConversation.id),
            ]);
            // await 期间用户可能已切走，避免把旧对话的删除结果写入新对话。
            if (get().currentConversation?.id !== currentConversation.id) return;
            const newCurrentNodeId = treeData.current_node_id || result.new_current_node_id;
            set((state) => ({
              branches,
              treeData,
              currentNodeId: newCurrentNodeId,
              currentConversation: state.currentConversation
                ? { ...state.currentConversation, current_node_id: newCurrentNodeId }
                : state.currentConversation,
              conversations: state.conversations.map((conversation) =>
                conversation.id === currentConversation.id
                  ? { ...conversation, current_node_id: newCurrentNodeId }
                  : conversation
              ),
            }));
          } finally {
            set({ loading: false });
          }
        },

        loadTree: async (conversationId: string) => {
          const inFlight = loadTreeInFlight.get(conversationId);
          if (inFlight) return inFlight;
          const request = (async () => {
            try {
              const data = await conversationApi.getTree(conversationId);
              set({ treeData: data });
            } finally {
              loadTreeInFlight.delete(conversationId);
            }
          })();
          loadTreeInFlight.set(conversationId, request);
          return request;
        },

      }),
      {
        name: conversationStorageKey,
        partialize: (state) => ({ conversations: state.conversations }),
        onRehydrateStorage: () => (state) => {
          if (state) state.currentConversation = null;
        },
      }
    )
  )
);

// 直接导出 zustand store hook（与 conversationStore 别名、modelStore/navigationStore 一致），
// 保留 selector 重载 useConversationStore((s) => ...) 与静态 useConversationStore.getState()。
// 此前用 `() => useConversationStoreBase()` 包装会丢失这两者，导致按 selector 订阅失效
// 且 .getState() 不存在（ChatInput/TreeView/MainPage 多处调用）。
export const useConversationStore = useConversationStoreBase;
export const conversationStore = useConversationStoreBase;
