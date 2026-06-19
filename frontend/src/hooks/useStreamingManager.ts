import { useCallback, useSyncExternalStore } from 'react';
import type { SendMessageRequest } from '../types/message';
import { streamManager } from '../services/streamManager';

/**
 * 纯读取 Hook：把 StreamManager 中某个对话的流状态投射到 React。
 *
 * 完成处理（刷新消息、错误落地）不再由组件回调驱动，而是由 StreamManager
 * 的 onFinish 全局事件负责（见 MainPage 中的注册）。这样即使用户切走、
 * Hook 已解绑该对话，流完成时仍能正确刷新——真正支持多对话并发。
 */
export function useStreamingManager(conversationId: string | null) {
  // 通过 useSyncExternalStore 从 StreamManager 读取，保证 tear-free。
  const state = useSyncExternalStore(
    useCallback(
      (callback: () => void) => {
        if (!conversationId) return () => {};
        return streamManager.subscribe((changedId) => {
          // 只在本对话变化时触发重渲染，避免并发流互相打扰。
          if (changedId === conversationId) callback();
        });
      },
      [conversationId]
    ),
    useCallback(() => {
      if (!conversationId) return undefined;
      return streamManager.getState(conversationId);
    }, [conversationId])
  );

  const isStreaming = state?.status === 'streaming';
  const streamedContent = state?.content ?? '';
  const streamedReasoning = state?.reasoning ?? '';
  const streamedReasoningActive = state?.reasoningActive ?? false;
  const streamedToolInteractions = state?.toolInteractions ?? [];
  const currentNodeId = state?.nodeId ?? null;
  const streamDuration = state?.duration ?? 0;
  const streamStatus = state?.status ?? 'idle';
  const pendingUserMessage = state?.pendingUserMessage ?? null;

  const startStreaming = useCallback(
    async (
      convId: string,
      request: SendMessageRequest,
      pending: string | null = null,
      nodeId?: string,
    ) => {
      await streamManager.startStream(convId, request, pending, nodeId);
    },
    []
  );

  const abortStreaming = useCallback(() => {
    if (conversationId) {
      streamManager.stopStream(conversationId);
    }
  }, [conversationId]);

  const reset = useCallback(() => {
    // 清理本对话在 StreamManager 中的残留状态
    if (conversationId) {
      streamManager.cleanup(conversationId);
    }
  }, [conversationId]);

  return {
    isStreaming,
    streamedContent,
    streamedReasoning,
    streamedReasoningActive,
    streamedToolInteractions,
    currentNodeId,
    streamDuration,
    streamStatus,
    pendingUserMessage,
    startStreaming,
    abortStreaming,
    reset,
  };
}
