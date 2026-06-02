import { useState, useCallback } from 'react';
import { messageApi } from '../api/message';
import { conversationApi } from '../api/conversation';
import type { Conversation, ConversationCreateRequest } from '../types/conversation';
import type { Message } from '../types/message';

export const useConversation = () => {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentConversation, setCurrentConversation] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [branches, setBranches] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  // 加载对话列表
  const loadConversations = useCallback(async () => {
    setLoading(true);
    try {
      const data = await conversationApi.list();
      setConversations(data);
    } catch (err) {
      setError(err as Error);
    } finally {
      setLoading(false);
    }
  }, []);

  // 创建对话
  const createConversation = useCallback(async (request?: ConversationCreateRequest) => {
    setLoading(true);
    try {
      const conversation = await conversationApi.create(request);
      setCurrentConversation(conversation);
      setMessages([]);
      return conversation;
    } catch (err) {
      setError(err as Error);
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  // 加载特定对话
  const loadConversation = useCallback(async (id: string) => {
    setLoading(true);
    try {
      // 先加载对话（如果需要）
      const conv = conversations.find((c) => c.id === id);
      if (conv) {
        setCurrentConversation(conv);
      }

      // 加载消息历史
      const history = await messageApi.getHistory(id);
      setMessages(history);

      // 加载分支信息
      const branchesData = await conversationApi.getBranches(id);
      setBranches(branchesData);
    } catch (err) {
      setError(err as Error);
    } finally {
      setLoading(false);
    }
  }, [conversations]);

  // 删除对话
  const deleteConversation = useCallback(
    async (id: string) => {
      try {
        await conversationApi.delete(id);
        setConversations((prev) => prev.filter((c) => c.id !== id));
        if (currentConversation?.id === id) {
          setCurrentConversation(null);
          setMessages([]);
        }
      } catch (err) {
        setError(err as Error);
        throw err;
      }
    },
    [currentConversation]
  );

  // 切换节点
  const switchNode = useCallback(
    async (nodeId: string) => {
      if (!currentConversation) return;
      try {
        await conversationApi.switchNode(currentConversation.id, nodeId);
        await loadConversation(currentConversation.id);
      } catch (err) {
        setError(err as Error);
        throw err;
      }
    },
    [currentConversation, loadConversation]
  );

  // 发送消息（非流式）
  const sendMessage = useCallback(
    async (content: string, modelId?: string) => {
      if (!currentConversation) throw new Error('No conversation selected');
      try {
        const result = await messageApi.send(currentConversation.id, {
          content,
          model_id: modelId,
        });
        // 重新加载消息历史
        await loadConversation(currentConversation.id);
        return result;
      } catch (err) {
        setError(err as Error);
        throw err;
      }
    },
    [currentConversation, loadConversation]
  );

  // 删除节点（用于重试功能）
  const deleteNode = useCallback(
    async (nodeId: string) => {
      if (!currentConversation) throw new Error('No conversation selected');
      try {
        await conversationApi.deleteNode(currentConversation.id, nodeId);
        // 重新加载消息历史
        await loadConversation(currentConversation.id);
      } catch (err) {
        setError(err as Error);
        throw err;
      }
    },
    [currentConversation, loadConversation]
  );

  return {
    conversations,
    currentConversation,
    messages,
    branches,
    loading,
    error,
    loadConversations,
    createConversation,
    loadConversation,
    deleteConversation,
    switchNode,
    sendMessage,
    deleteNode,
    setCurrentConversation,
  };
};