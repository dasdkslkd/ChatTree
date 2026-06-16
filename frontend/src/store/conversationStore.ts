import { create } from 'zustand';
import { devtools, persist } from 'zustand/middleware';
import type {
  Conversation,
  ConversationCreateRequest,
} from '../types/conversation';
import { conversationApi, type TreeData } from '../api/conversation';
import type { Message } from '../types/message';
import { messageApi } from '../api/message';
import { useModelStore } from './modelStore';

interface ConversationState {
  conversations: Conversation[];
  currentConversation: Conversation | null;
  messages: Message[];
  branches: Record<string, any>;
  treeData: TreeData | null;
  streamingContent: string;
  currentNodeId: string | null;
  pendingScrollNodeId: string | null;
  isStreaming: boolean;
  loading: boolean;
  error: string | null;
}

interface ConversationActions {
  loadConversations: () => Promise<void>;
  createConversation: (request?: ConversationCreateRequest) => Promise<Conversation | null>;
  selectConversation: (id: string) => Promise<void>;
  deleteConversation: (id: string) => Promise<void>;
  updateConversationTitle: (id: string, title: string) => Promise<void>;
  clearCurrentConversation: () => void;
  switchNode: (nodeId: string) => Promise<void>;
  deleteNode: (nodeId: string) => Promise<void>;
  abortStreaming: () => void;
  clearError: () => void;
  loadTree: (conversationId: string) => Promise<void>;
  clearPendingScroll: () => void;
  refreshMessages: (conversationId: string, opts?: { awaitNodeId?: string; awaitRole?: 'assistant' | 'user'; retries?: number }) => Promise<boolean>;
}

const useConversationStoreBase = create<ConversationState & ConversationActions>()(
  devtools(
    persist(
      (set, get) => ({
        conversations: [],
        currentConversation: null,
        messages: [],
        branches: {},
        treeData: null,
        streamingContent: '',
        currentNodeId: null,
        pendingScrollNodeId: null,
        isStreaming: false,
        loading: false,
        error: null,

        loadConversations: async () => {
          set({ loading: true, error: null });
          try {
            const data = await conversationApi.list();
            set({ conversations: data });
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

        createConversation: async (request) => {
          set({ loading: true, error: null });
          try {
            const conversation = await conversationApi.create(request);
            set({
              currentConversation: conversation,
              messages: [],
              treeData: null,
              streamingContent: '',
              currentNodeId: null,
              pendingScrollNodeId: null,
            });
            await get().loadConversations();
            // 新建对话：重置为默认模型
            await useModelStore.getState().resetToDefault();
            return conversation;
          } catch (err: any) {
            set({ error: err.message });
            return null;
          } finally {
            set({ loading: false });
          }
        },

        selectConversation: async (id) => {
          set({ loading: true, error: null });
          try {
            const [history, branches] = await Promise.all([
              messageApi.getHistory(id),
              conversationApi.getBranches(id),
            ]);
            const conversation = get().conversations.find((c) => c.id === id);
            set({
              currentConversation: conversation || null,
              messages: history,
              branches: branches || {},
              treeData: null,
              streamingContent: '',
              currentNodeId: conversation?.current_node_id || null,
              pendingScrollNodeId: null,
            });
            // 同步模型选择到 modelStore
            await useModelStore.getState().syncFromConversation(
              conversation?.provider_id || null,
              conversation?.model_id || null,
            );
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

        deleteConversation: async (id) => {
          try {
            const isCurrent = get().currentConversation?.id === id;
            await conversationApi.delete(id);
            set((state) => ({
              conversations: state.conversations.filter((c) => c.id !== id),
              currentConversation: isCurrent ? null : state.currentConversation,
              messages: isCurrent ? [] : state.messages,
              branches: isCurrent ? {} : state.branches,
              treeData: isCurrent ? null : state.treeData,
              streamingContent: isCurrent ? '' : state.streamingContent,
              currentNodeId: isCurrent ? null : state.currentNodeId,
              pendingScrollNodeId: isCurrent ? null : state.pendingScrollNodeId,
            }));
            if (isCurrent) {
              await useModelStore.getState().resetToDefault();
            }
          } catch (err: any) {
            set({ error: err.message });
          }
        },

        updateConversationTitle: async (id, title) => {
          try {
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
          } catch (err: any) {
            set({ error: err.message });
          }
        },

        switchNode: async (nodeId) => {
          const { currentConversation } = get();
          if (!currentConversation) return;

          set({ loading: true, error: null });
          try {
            await conversationApi.switchNode(currentConversation.id, nodeId);
            const history = await messageApi.getHistory(currentConversation.id);
            const branches = await conversationApi.getBranches(currentConversation.id);

            set({
              messages: history,
              branches: branches || {},
              currentNodeId: nodeId,
              streamingContent: '',
              pendingScrollNodeId: nodeId,
            });
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

        abortStreaming: () => set({ isStreaming: false, streamingContent: '' }),
        clearError: () => set({ error: null }),
        clearPendingScroll: () => set({ pendingScrollNodeId: null }),

        // 流式结束后，从后端拉取真实消息。
        // 完成判据：等待 **本轮节点的指定角色消息** 落盘，而非“消息数 +1”。
        // 这样对多消息轮次（未来工具轮次：assistant tool_call + tool_result + final）
        // 同样稳健——只要本节点的 assistant 消息出现即认定完成。
        // 与 MainPage 的 assistantMsgLanded 判据（node_id + role）保持一致。
        // 返回值：true=已确认落地（可清理乐观状态）；false=未确认（已切走/出错/超时）。
        refreshMessages: async (
          conversationId: string,
          opts?: { awaitNodeId?: string; awaitRole?: 'assistant' | 'user'; retries?: number },
        ): Promise<boolean> => {
          if (get().currentConversation?.id !== conversationId) return false;
          const awaitNodeId = opts?.awaitNodeId;
          const awaitRole = opts?.awaitRole ?? 'assistant';
          const retries = opts?.retries ?? 0;
          const landed = (history: Message[]) =>
            !awaitNodeId || history.some((m) => m.node_id === awaitNodeId && m.role === awaitRole);
          for (let attempt = 0; attempt <= retries; attempt++) {
            try {
              const [history, branches] = await Promise.all([
                messageApi.getHistory(conversationId),
                conversationApi.getBranches(conversationId),
              ]);
              // 再次校验：await 期间用户可能已切走
              if (get().currentConversation?.id !== conversationId) return false;
              const ok = landed(history);
              if (ok || attempt === retries) {
                // 写入最新结果以保持一致。ok=true 时返回 true 让调用方清理乐观气泡；
                // 重试用尽仍未落地则返回 false，调用方保留气泡、择机再刷新。
                const conv = get().conversations.find((c) => c.id === conversationId);
                set({
                  messages: history,
                  branches: branches || {},
                  currentNodeId: conv?.current_node_id || get().currentNodeId,
                });
                return ok;
              }
              // 后端尚未保存完成，稍候重试（保留乐观气泡）
              await new Promise((r) => setTimeout(r, 150));
            } catch (err: any) {
              set({ error: err.message });
              return false;
            }
          }
          return false;
        },

        clearCurrentConversation: () => {
          set({
            currentConversation: null,
            messages: [],
            branches: {},
            treeData: null,
            streamingContent: '',
            currentNodeId: null,
            pendingScrollNodeId: null,
          });
          // 清空对话时重置为默认模型
          useModelStore.getState().resetToDefault();
        },

        deleteNode: async (nodeId) => {
          const { currentConversation } = get();
          if (!currentConversation) return;
          set({ loading: true, error: null });
          try {
            const result = await conversationApi.deleteNode(currentConversation.id, nodeId);
            if (result.new_current_node_id) set({ currentNodeId: result.new_current_node_id });
            const history = await messageApi.getHistory(currentConversation.id);
            const branches = await conversationApi.getBranches(currentConversation.id);
            set({ messages: history, branches: branches || {} });
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

        loadTree: async (conversationId: string) => {
          try {
            const data = await conversationApi.getTree(conversationId);
            set({ treeData: data });
          } catch (err: any) {
            set({ error: err.message });
          }
        },

      }),
      {
        name: 'conversation-storage',
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

