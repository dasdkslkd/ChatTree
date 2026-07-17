import { create } from 'zustand';
import { devtools, persist } from 'zustand/middleware';
import type {
  Conversation,
  ConversationCreateRequest,
  MultiAgentMode,
} from '../types/conversation';
import { conversationApi, type TreeData } from '../api/conversation';
import { ChatTreeApiError } from '../api/errors';
import type { Message } from '../types/message';
import { messageApi } from '../api/message';
import { useModelStore } from './modelStore';
import {
  StaleConnectionEpochError,
  captureConnectionEpoch,
  commitForConnectionEpoch as commitForConnectionEpochStrict,
  connectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';
import { getFrontendBootstrap } from '../runtime/frontendBootstrap';
import {
  CONVERSATION_STORAGE_KEY,
  profileStorageKey,
} from '../runtime/profileStorage';

const conversationStorageKey = profileStorageKey(
  getFrontendBootstrap().profileId,
  CONVERSATION_STORAGE_KEY,
);

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
  loadConversations: (token?: ConnectionEpochToken) => Promise<void>;
  createConversation: (request?: ConversationCreateRequest, token?: ConnectionEpochToken) => Promise<Conversation | null>;
  selectConversation: (id: string, token?: ConnectionEpochToken) => Promise<void>;
  deleteConversation: (id: string, token?: ConnectionEpochToken) => Promise<void>;
  updateConversationTitle: (id: string, title: string, token?: ConnectionEpochToken) => Promise<void>;
  updateConversationModel: (
    id: string,
    modelId: string,
    providerId: string,
    reasoningEffort?: string | null,
    thinkingEnabled?: boolean | null,
    token?: ConnectionEpochToken,
  ) => Promise<boolean>;
  updateMultiAgentMode: (id: string, mode: MultiAgentMode, token?: ConnectionEpochToken) => Promise<void>;
  clearCurrentConversation: (token?: ConnectionEpochToken) => Promise<void>;
  switchNode: (nodeId: string, token?: ConnectionEpochToken) => Promise<void>;
  setCurrentNodeIdLocal: (nodeId: string) => void;
  deleteNode: (nodeId: string, token?: ConnectionEpochToken) => Promise<void>;
  abortStreaming: () => void;
  clearError: () => void;
  loadTree: (conversationId: string, token?: ConnectionEpochToken) => Promise<void>;
  clearPendingScroll: () => void;
  refreshMessages: (conversationId: string, opts?: { awaitNodeId?: string; awaitRole?: 'assistant' | 'user'; retries?: number }, token?: ConnectionEpochToken) => Promise<boolean>;
  refreshBranches: (conversationId: string, token?: ConnectionEpochToken) => Promise<boolean>;
  patchAssistantMessageFromStream: (conversationId: string, message: Message, pendingUserContent?: string | null) => boolean;
}

function isActiveRunDeleteConflict(err: unknown): boolean {
  return err instanceof ChatTreeApiError
    && err.status === 409
    && err.code === 'active_runs_present';
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

function waitForEpochRetry(token: ConnectionEpochToken, delayMs: number): Promise<void> {
  connectionEpochRuntime.assertCurrent(token);
  const signal = connectionEpochRuntime.signalFor(token);
  if (signal.aborted) return Promise.reject(new StaleConnectionEpochError());
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, delayMs);
    const onAbort = () => {
      clearTimeout(timeout);
      reject(new StaleConnectionEpochError());
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

async function deleteNodeAllowingActiveRuns(
  conversationId: string,
  nodeId: string,
  token: ConnectionEpochToken,
) {
  try {
    return await conversationApi.deleteNode(conversationId, nodeId);
  } catch (err: any) {
    if (!isActiveRunDeleteConflict(err)) throw err;
    connectionEpochRuntime.assertCurrent(token);
    return await conversationApi.deleteNode(conversationId, nodeId, { force: true });
  }
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

        loadConversations: async (ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            set({ loading: true, error: null });
            const data = await conversationApi.list();
            commitForConnectionEpoch(token, () => set({ conversations: data }));
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
          } finally {
            if (token) commitForConnectionEpoch(token, () => set({ loading: false }));
          }
        },

        createConversation: async (request, ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            set({ loading: true, error: null });
            const conversation = await conversationApi.create(request);
            connectionEpochRuntime.assertCurrent(token);
            commitForConnectionEpoch(token, () => set({
              currentConversation: conversation,
              messages: [],
              treeData: null,
              streamingContent: '',
              currentNodeId: null,
              pendingScrollNodeId: null,
            }));
            await get().loadConversations(token);
            connectionEpochRuntime.assertCurrent(token);
            // 保留用户已选模型：新建对话前若用户已在模型框里选过模型（store 里有
            // current 值），把它持久化到新对话，并保持 store 不变——否则
            // resetToDefault() 会把按钮显示回退到默认模型（请求仍用已选模型，但
            // 显示串掉）。仅当 store 没有任何选择时才回退默认作为初始化。
            const ms = useModelStore.getState();
            if (ms.currentProvider && ms.currentModel) {
              try {
                await conversationApi.updateModel(
                  conversation.id,
                  ms.currentModel,
                  ms.currentProvider,
                  ms.currentReasoningEffort,
                  ms.currentThinkingEnabled,
                );
                connectionEpochRuntime.assertCurrent(token);
                const updatedConversation = {
                  ...conversation,
                  model_id: ms.currentModel,
                  provider_id: ms.currentProvider,
                  reasoning_effort: ms.currentReasoningEffort,
                  thinking_enabled: ms.currentThinkingEnabled,
                };
                commitForConnectionEpoch(token, () => set((state) => ({
                  conversations: state.conversations.map((item) => (
                    item.id === conversation.id ? { ...item, ...updatedConversation } : item
                  )),
                  currentConversation: state.currentConversation?.id === conversation.id
                    ? { ...state.currentConversation, ...updatedConversation }
                    : state.currentConversation,
                })));
                return updatedConversation;
              } catch (error) {
                if (isStaleEpoch(error, token)) throw error;
                // 持久化失败不阻断创建；store 选择仍保留，显示不会串
              }
            } else {
              await ms.resetToDefault(token);
              connectionEpochRuntime.assertCurrent(token);
            }
            return conversation;
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return null;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
            return null;
          } finally {
            if (token) commitForConnectionEpoch(token, () => set({ loading: false }));
          }
        },

        selectConversation: async (id, ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            set({ loading: true, error: null });
            const [history, branches] = await Promise.all([
              messageApi.getHistory(id),
              conversationApi.getBranches(id),
            ]);
            connectionEpochRuntime.assertCurrent(token);
            const conversation = get().conversations.find((c) => c.id === id);
            const currentNodeId = latestNodeIdFromHistory(history) || conversation?.current_node_id || null;
            commitForConnectionEpoch(token, () => set({
              currentConversation: conversation
                ? { ...conversation, current_node_id: currentNodeId || conversation.current_node_id }
                : null,
              messages: history,
              branches: branches || {},
              treeData: null,
              streamingContent: '',
              currentNodeId,
              pendingScrollNodeId: null,
            }));
            // 同步模型选择到 modelStore
            await useModelStore.getState().syncFromConversation(
              conversation?.provider_id || null,
              conversation?.model_id || null,
              undefined,
              undefined,
              token,
            );
            connectionEpochRuntime.assertCurrent(token);
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
          } finally {
            if (token) commitForConnectionEpoch(token, () => set({ loading: false }));
          }
        },

        deleteConversation: async (id, ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            const isCurrent = get().currentConversation?.id === id;
            await conversationApi.delete(id);
            connectionEpochRuntime.assertCurrent(token);
            commitForConnectionEpoch(token, () => set((state) => ({
              conversations: state.conversations.filter((c) => c.id !== id),
              currentConversation: isCurrent ? null : state.currentConversation,
              messages: isCurrent ? [] : state.messages,
              branches: isCurrent ? {} : state.branches,
              treeData: isCurrent ? null : state.treeData,
              streamingContent: isCurrent ? '' : state.streamingContent,
              currentNodeId: isCurrent ? null : state.currentNodeId,
              pendingScrollNodeId: isCurrent ? null : state.pendingScrollNodeId,
            })));
            if (isCurrent) {
              await useModelStore.getState().resetToDefault(token);
              connectionEpochRuntime.assertCurrent(token);
            }
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
          }
        },

        updateConversationTitle: async (id, title, ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            await conversationApi.updateTitle(id, title);
            connectionEpochRuntime.assertCurrent(token);
            commitForConnectionEpoch(token, () => set((state) => ({
              conversations: state.conversations.map((c) =>
                c.id === id ? { ...c, title } : c
              ),
              currentConversation:
                state.currentConversation?.id === id
                  ? { ...state.currentConversation, title }
                  : state.currentConversation,
            })));
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
          }
        },

        updateConversationModel: async (
          id,
          modelId,
          providerId,
          reasoningEffort,
          thinkingEnabled,
          ownerToken,
        ) => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            await conversationApi.updateModel(
              id,
              modelId,
              providerId,
              reasoningEffort,
              thinkingEnabled,
            );
            connectionEpochRuntime.assertCurrent(token);
            commitForConnectionEpoch(token, () => set((state) => ({
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
            })));
            return true;
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return false;
            throw err;
          }
        },

        updateMultiAgentMode: async (id, mode, ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            await conversationApi.updateMultiAgentMode(id, mode);
            connectionEpochRuntime.assertCurrent(token);
            commitForConnectionEpoch(token, () => set((state) => ({
              conversations: state.conversations.map((conversation) =>
                conversation.id === id ? { ...conversation, multi_agent_mode: mode } : conversation
              ),
              currentConversation:
                state.currentConversation?.id === id
                  ? { ...state.currentConversation, multi_agent_mode: mode }
                  : state.currentConversation,
            })));
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
          }
        },

        switchNode: async (nodeId, ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          const { currentConversation } = get();
          if (!currentConversation) return;

          try {
            token = resolveEpochToken(ownerToken);
            set({ loading: true, error: null });
            await conversationApi.switchNode(currentConversation.id, nodeId);
            connectionEpochRuntime.assertCurrent(token);
            const history = await messageApi.getHistory(currentConversation.id);
            connectionEpochRuntime.assertCurrent(token);
            const branches = await conversationApi.getBranches(currentConversation.id);
            connectionEpochRuntime.assertCurrent(token);

            commitForConnectionEpoch(token, () => set((state) => ({
              messages: history,
              branches: branches || {},
              currentNodeId: nodeId,
              currentConversation: state.currentConversation
                ? { ...state.currentConversation, current_node_id: nodeId }
                : state.currentConversation,
              conversations: state.conversations.map((conversation) =>
                conversation.id === currentConversation.id
                  ? { ...conversation, current_node_id: nodeId }
                  : conversation
              ),
              streamingContent: '',
              pendingScrollNodeId: nodeId,
            })));
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
          } finally {
            if (token) commitForConnectionEpoch(token, () => set({ loading: false }));
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

        abortStreaming: () => set({ isStreaming: false, streamingContent: '' }),
        clearError: () => set({ error: null }),
        clearPendingScroll: () => set({ pendingScrollNodeId: null }),

        patchAssistantMessageFromStream: (
          conversationId: string,
          message: Message,
          pendingUserContent?: string | null,
        ): boolean => {
          if (get().currentConversation?.id !== conversationId || message.role !== 'assistant' || !message.node_id) {
            return false;
          }
          let patched = false;
          set((state) => {
            if (state.currentConversation?.id !== conversationId) return state;
            let replaced = false;
            const messages = state.messages.map((existing) => {
              const sameAssistantNode = existing.role === 'assistant' && existing.node_id === message.node_id;
              const sameMessageId = existing.id === message.id;
              if (!sameAssistantNode && !sameMessageId) return existing;
              replaced = true;
              return {
                ...existing,
                ...message,
                id: existing.id || message.id,
                timestamp: existing.timestamp || message.timestamp,
              };
            });
            const hasUserMessage = messages.some((existing) =>
              existing.role === 'user' && existing.node_id === message.node_id
            );
            const userContent = pendingUserContent?.trim();
            if (!hasUserMessage && userContent) {
              messages.push({
                id: `stream-user-${message.id}`,
                role: 'user',
                content: pendingUserContent ?? '',
                node_id: message.node_id,
                parent_node_id: message.parent_node_id,
                timestamp: Math.max(0, message.timestamp - 0.001),
              });
            }
            if (!replaced) messages.push(message);
            patched = true;
            const currentNodeId = message.node_id || state.currentNodeId;
            return {
              messages,
              currentNodeId,
              currentConversation: state.currentConversation
                ? { ...state.currentConversation, current_node_id: currentNodeId || state.currentConversation.current_node_id }
                : state.currentConversation,
              conversations: state.conversations.map((conversation) =>
                conversation.id === conversationId
                  ? { ...conversation, current_node_id: currentNodeId || conversation.current_node_id }
                  : conversation
              ),
            };
          });
          return patched;
        },

        // 流式结束后，从后端拉取真实消息。
        // 完成判据：等待 **本轮节点的指定角色消息** 落盘，而非“消息数 +1”。
        // 这样对多消息轮次（未来工具轮次：assistant tool_call + tool_result + final）
        // 同样稳健——只要本节点的 assistant 消息出现即认定完成。
        // 与 MainPage 的 assistantMsgLanded 判据（node_id + role）保持一致。
        // 返回值：true=已确认落地（可清理乐观状态）；false=未确认（已切走/出错/超时）。
        refreshMessages: async (
          conversationId: string,
          opts?: { awaitNodeId?: string; awaitRole?: 'assistant' | 'user'; retries?: number },
          ownerToken?: ConnectionEpochToken,
        ): Promise<boolean> => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            if (get().currentConversation?.id !== conversationId) return false;
            const awaitNodeId = opts?.awaitNodeId;
            const awaitRole = opts?.awaitRole ?? 'assistant';
            const retries = opts?.retries ?? 0;
            const landed = (history: Message[]) =>
              !awaitNodeId || history.some((m) => m.node_id === awaitNodeId && m.role === awaitRole);
            for (let attempt = 0; attempt <= retries; attempt++) {
              const history = await messageApi.getHistory(conversationId);
              connectionEpochRuntime.assertCurrent(token);
              // 再次校验：await 期间用户可能已切走
              if (get().currentConversation?.id !== conversationId) return false;
              const ok = landed(history);
              if (ok || attempt === retries) {
                if (!ok && awaitNodeId) {
                  return false;
                }
                // 写入最新结果以保持一致。ok=true 时返回 true 让调用方清理乐观气泡；
                // 重试用尽仍未落地则返回 false，调用方保留气泡、择机再刷新。
                const conv = get().conversations.find((c) => c.id === conversationId);
                const currentNodeId = latestNodeIdFromHistory(history) || conv?.current_node_id || get().currentNodeId;
                commitForConnectionEpoch(token, () => set((state) => ({
                  messages: history,
                  currentNodeId,
                  currentConversation: state.currentConversation?.id === conversationId
                    ? { ...state.currentConversation, current_node_id: currentNodeId || state.currentConversation.current_node_id }
                    : state.currentConversation,
                  conversations: state.conversations.map((conversation) =>
                    conversation.id === conversationId
                      ? { ...conversation, current_node_id: currentNodeId || conversation.current_node_id }
                      : conversation
                  ),
                })));
                return ok;
              }
              // 后端尚未保存完成，稍候重试（保留乐观气泡）
              await waitForEpochRetry(token, 150);
              connectionEpochRuntime.assertCurrent(token);
            }
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return false;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
            return false;
          }
          return false;
        },

        refreshBranches: async (conversationId: string, ownerToken?: ConnectionEpochToken): Promise<boolean> => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            if (get().currentConversation?.id !== conversationId) return false;
            const branches = await conversationApi.getBranches(conversationId);
            connectionEpochRuntime.assertCurrent(token);
            if (get().currentConversation?.id !== conversationId) return false;
            commitForConnectionEpoch(token, () => set({ branches: branches || {} }));
            return true;
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return false;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
            return false;
          }
        },

        clearCurrentConversation: async (ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            commitForConnectionEpoch(token, () => set({
              currentConversation: null,
              messages: [],
              branches: {},
              treeData: null,
              streamingContent: '',
              currentNodeId: null,
              pendingScrollNodeId: null,
            }));
            await useModelStore.getState().resetToDefault(token);
            connectionEpochRuntime.assertCurrent(token);
          } catch (error) {
            if (!isStaleEpoch(error, token)) {
              commitForConnectionEpoch(token, () => set({ error: errorMessage(error) }));
            }
          }
        },

        deleteNode: async (nodeId, ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          const { currentConversation } = get();
          if (!currentConversation) return;
          try {
            token = resolveEpochToken(ownerToken);
            set({ loading: true, error: null });
            const result = await deleteNodeAllowingActiveRuns(currentConversation.id, nodeId, token);
            connectionEpochRuntime.assertCurrent(token);
            const [history, branches, treeData] = await Promise.all([
              messageApi.getHistory(currentConversation.id),
              conversationApi.getBranches(currentConversation.id),
              conversationApi.getTree(currentConversation.id),
            ]);
            connectionEpochRuntime.assertCurrent(token);
            // await 期间用户可能已切走，避免把旧对话的删除结果写入新对话。
            if (get().currentConversation?.id !== currentConversation.id) return;
            const newCurrentNodeId = treeData.current_node_id || result.new_current_node_id;
            commitForConnectionEpoch(token, () => set((state) => ({
              messages: history,
              branches: branches || {},
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
            })));
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
          } finally {
            if (token) commitForConnectionEpoch(token, () => set({ loading: false }));
          }
        },

        loadTree: async (conversationId: string, ownerToken) => {
          let token: ConnectionEpochToken | null = null;
          try {
            token = resolveEpochToken(ownerToken);
            const data = await conversationApi.getTree(conversationId);
            connectionEpochRuntime.assertCurrent(token);
            commitForConnectionEpoch(token, () => set({ treeData: data }));
          } catch (err: unknown) {
            if (isStaleEpoch(err, token)) return;
            commitForConnectionEpoch(token, () => set({ error: errorMessage(err) }));
          }
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

function latestNodeIdFromHistory(history: Message[]): string | null {
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const nodeId = history[index]?.node_id;
    if (nodeId) return nodeId;
  }
  return null;
}

// 直接导出 zustand store hook（与 conversationStore 别名、modelStore/navigationStore 一致），
// 保留 selector 重载 useConversationStore((s) => ...) 与静态 useConversationStore.getState()。
// 此前用 `() => useConversationStoreBase()` 包装会丢失这两者，导致按 selector 订阅失效
// 且 .getState() 不存在（ChatInput/TreeView/MainPage 多处调用）。
export const useConversationStore = useConversationStoreBase;
export const conversationStore = useConversationStoreBase;
