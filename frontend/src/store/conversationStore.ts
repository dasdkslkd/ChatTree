import { create } from 'zustand';
import { devtools, persist } from 'zustand/middleware';
import type {
  Conversation,
  ConversationCreateRequest,
} from '../types/conversation';
import { conversationApi, type TreeData } from '../api/conversation';
import type { Message } from '../types/message';
import { messageApi } from '../api/message';

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
  sendMessage: (content: string, modelId?: string) => Promise<void>;
  streamMessage: (content: string, modelId?: string) => Promise<void>;
  deleteNode: (nodeId: string) => Promise<void>;
  abortStreaming: () => void;
  clearError: () => void;
  loadTree: (conversationId: string) => Promise<void>;
  clearPendingScroll: () => void;
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
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

        deleteConversation: async (id) => {
          try {
            await conversationApi.delete(id);
            set((state) => {
              const isCurrent = state.currentConversation?.id === id;
              return {
                conversations: state.conversations.filter((c) => c.id !== id),
                currentConversation: isCurrent ? null : state.currentConversation,
                messages: isCurrent ? [] : state.messages,
                branches: isCurrent ? {} : state.branches,
                treeData: isCurrent ? null : state.treeData,
                streamingContent: isCurrent ? '' : state.streamingContent,
                currentNodeId: isCurrent ? null : state.currentNodeId,
                pendingScrollNodeId: isCurrent ? null : state.pendingScrollNodeId,
              };
            });
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
              if (chunk.content) {
                set((state) => ({
                  streamingContent: state.streamingContent + chunk.content,
                  currentNodeId: chunk.node_id || state.currentNodeId,
                }));
              }

              if (chunk.status === 'complete') break;
              else if (chunk.status === 'error') throw new Error(chunk.error || 'Stream error');
            }

            await new Promise((resolve) => setTimeout(resolve, 300));
            const history = await messageApi.getHistory(currentConversation.id);
            const branches = await conversationApi.getBranches(currentConversation.id);
            set({ messages: history, branches: branches || {}, streamingContent: '', isStreaming: false });
          } catch (err: any) {
            if (err.name !== 'AbortError') {
              const history = await messageApi.getHistory(currentConversation.id);
              const branches = await conversationApi.getBranches(currentConversation.id);
              set({ error: err.message, isStreaming: false, streamingContent: '', messages: history, branches: branches || {} });
            }
          }
        },

        sendMessage: async (content, modelId) => {
          const { currentConversation } = get();
          if (!currentConversation) return;
          set({ loading: true, error: null });
          try {
            await messageApi.send(currentConversation.id, { content, model_id: modelId });
            await get().selectConversation(currentConversation.id);
          } catch (err: any) {
            set({ error: err.message });
          } finally {
            set({ loading: false });
          }
        },

        abortStreaming: () => set({ isStreaming: false, streamingContent: '' }),
        clearError: () => set({ error: null }),
        clearPendingScroll: () => set({ pendingScrollNodeId: null }),

        clearCurrentConversation: () => set({
          currentConversation: null,
          messages: [],
          branches: {},
          treeData: null,
          streamingContent: '',
          currentNodeId: null,
          pendingScrollNodeId: null,
        }),

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

export const useConversationStore = () => useConversationStoreBase();
export const conversationStore = useConversationStoreBase;

