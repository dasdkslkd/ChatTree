import { create } from 'zustand';
import { devtools, persist } from 'zustand/middleware';
import type {
  Conversation,
  ConversationCreateRequest,
} from '../types/conversation';
import { conversationApi } from '../api/conversation';
import type { Message } from '../types/message';
import { messageApi } from '../api/message';

interface ConversationState {
  conversations: Conversation[];
  currentConversation: Conversation | null;
  messages: Message[];
  branches: Record<string, any>;
  streamingContent: string;
  currentNodeId: string | null;
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
  sendMessage: (content: string, modelId?: string) => Promise<void>;
  streamMessage: (content: string, modelId?: string) => Promise<void>;
  deleteNode: (nodeId: string) => Promise<void>;
  abortStreaming: () => void;
  clearError: () => void;
}

const useConversationStoreBase = create<ConversationState & ConversationActions>()(
  devtools(
    persist(
      (set, get) => ({
        conversations: [],
        currentConversation: null,
        messages: [],
        branches: {},
        streamingContent: '',
        currentNodeId: null,
        isStreaming: false,
        loading: false,
        error: null,

        // 加载对话列表
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

        // 创建对话（修复：创建后自动选中）
        createConversation: async (request) => {
          set({ loading: true, error: null });
          try {
            const conversation = await conversationApi.create(request);
            set({
              currentConversation: conversation, // 恢复自动选中
              messages: [],
              streamingContent: '',
              currentNodeId: null,
            });
            // 重新加载列表
            await get().loadConversations();
            return conversation;
          } catch (err: any) {
            set({ error: err.message });
            return null;
          } finally {
            set({ loading: false });
          }
        },

        // 选择对话
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
              streamingContent: '',
              currentNodeId: conversation?.current_node_id || null,
            });
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

        // 删除对话
        deleteConversation: async (id) => {
          try {
            await conversationApi.delete(id);
            set((state) => {
              const isCurrentConversation = state.currentConversation?.id === id;
              return {
                conversations: state.conversations.filter((c) => c.id !== id),
                currentConversation: isCurrentConversation ? null : state.currentConversation,
                // 如果删除的是当前对话，同时清空消息和分支
                messages: isCurrentConversation ? [] : state.messages,
                branches: isCurrentConversation ? {} : state.branches,
                streamingContent: isCurrentConversation ? '' : state.streamingContent,
                currentNodeId: isCurrentConversation ? null : state.currentNodeId,
              };
            });
          } catch (err: any) {
            set({ error: err.message });
          }
        },

        // 更新对话标题
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

        // 切换节点
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
            });
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

        // 发送消息（流式）
        streamMessage: async (content, modelId) => {
          const { currentConversation } = get();
          if (!currentConversation) {
            set({ error: 'No conversation selected' });
            return;
          }

          set({ isStreaming: true, error: null, streamingContent: '' });

          try {
            for await (const chunk of messageApi.stream(currentConversation.id, {
              content,
              model_id: modelId,
            })) {
              // 只有在有内容时才追加
              if (chunk.content) {
                set((state) => ({
                  streamingContent: state.streamingContent + chunk.content,
                  currentNodeId: chunk.node_id || state.currentNodeId,
                }));
              }

              if (chunk.status === 'complete') {
                break;
              } else if (chunk.status === 'error') {
                throw new Error(chunk.error || 'Stream error');
              }
            }

            await new Promise((resolve) => setTimeout(resolve, 300));
            const history = await messageApi.getHistory(currentConversation.id);
            const branches = await conversationApi.getBranches(currentConversation.id);

            set({
              messages: history,
              branches: branches || {},
              streamingContent: '',
              isStreaming: false,
            });
          } catch (err: any) {
            if (err.name !== 'AbortError') {
              // Reload history to show saved content
              const history = await messageApi.getHistory(currentConversation.id);
              const branches = await conversationApi.getBranches(currentConversation.id);
              set({
                error: err.message,
                isStreaming: false,
                streamingContent: '',
                messages: history,
                branches: branches || {},
              });
            }
          }
        },

        // 普通发送（备用）
        sendMessage: async (content, modelId) => {
          const { currentConversation } = get();
          if (!currentConversation) return;

          set({ loading: true, error: null });
          try {
            await messageApi.send(currentConversation.id, {
              content,
              model_id: modelId,
            });
            await get().selectConversation(currentConversation.id);
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

        // 中止流式
        abortStreaming: () => {
          set({ isStreaming: false, streamingContent: '' });
        },

        // 清除错误
        clearError: () => set({ error: null }),

        // 清空当前对话选择
        clearCurrentConversation: () => set({
          currentConversation: null,
          messages: [],
          branches: {},
          streamingContent: '',
          currentNodeId: null,
        }),

        // 删除节点
        deleteNode: async (nodeId) => {
          const { currentConversation } = get();
          if (!currentConversation) return;

          set({ loading: true, error: null });
          try {
            const result = await conversationApi.deleteNode(currentConversation.id, nodeId);
            // 更新当前节点ID为父节点
            if (result.new_current_node_id) {
              set({ currentNodeId: result.new_current_node_id });
            }
            // 重新加载消息历史
            const history = await messageApi.getHistory(currentConversation.id);
            const branches = await conversationApi.getBranches(currentConversation.id);
            set({
              messages: history,
              branches: branches || {},
            });
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

      }),
      {
        name: 'conversation-storage',
        // 不持久化currentConversation，刷新后不自动选中
        partialize: (state) => ({
          conversations: state.conversations,
        }),
        // 恢复后清空选中状态
        onRehydrateStorage: () => (state) => {
          if (state) {
            state.currentConversation = null;
          }
        },
      }
    )
  )
);

export const useConversationStore = () => useConversationStoreBase();