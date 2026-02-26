import { useState, useCallback, useEffect, useRef } from 'react';
import type { StreamChunk, SendMessageRequest } from '../types/message';
import { messageApi } from '../api/message';

interface UseStreamingOptions {
  onChunk?: (chunk: StreamChunk, conversationId: string) => void;
  onComplete?: (fullContent: string, conversationId: string) => void;
  onError?: (error: Error, conversationId: string) => void;
}

export const useStreaming = (options: UseStreamingOptions = {}) => {
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamedContent, setStreamedContent] = useState('');
  const [currentNodeId, setCurrentNodeId] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [tokensUsed, setTokensUsed] = useState(0);
  // 跟踪当前流式请求的对话 ID
  const [streamingConversationId, setStreamingConversationId] = useState<string | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  const currentConversationIdRef = useRef<string | null>(null);
  
  // 使用 ref 存储回调，避免闭包问题
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const startStreaming = useCallback(
    async (conversationId: string, request: SendMessageRequest) => {
      // 重置状态
      setIsStreaming(true);
      setStreamedContent('');
      setError(null);
      setTokensUsed(0);
      setCurrentNodeId(null);
      setStreamingConversationId(conversationId);
      currentConversationIdRef.current = conversationId;

      abortControllerRef.current = new AbortController();
      
      let fullContent = '';

      try {
        for await (const chunk of messageApi.stream(conversationId, request)) {
          if (abortControllerRef.current?.signal.aborted) {
            break;
          }

          // 只有当 chunk.content 有值时才拼接
          if (chunk.content) {
            fullContent += chunk.content;
            setStreamedContent(fullContent);
          }
          if (chunk.node_id) {
            setCurrentNodeId(chunk.node_id);
          }
          if (chunk.tokens_used) {
            setTokensUsed(chunk.tokens_used);
          }
          optionsRef.current.onChunk?.(chunk, conversationId);

          if (chunk.status === 'complete' || chunk.status === 'stopped') {
            break;
          } else if (chunk.status === 'error') {
            throw new Error(chunk.error || 'Stream error occurred');
          }
        }

        setIsStreaming(false);
        optionsRef.current.onComplete?.(fullContent, conversationId);
      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') {
          setIsStreaming(false);
          return;
        }
        setError(err as Error);
        setIsStreaming(false);
        optionsRef.current.onError?.(err as Error, conversationId);
      }
    },
    [] // 空依赖数组，因为使用了 ref
  );

  const abortStreaming = useCallback(async () => {
    if (abortControllerRef.current && isStreaming) {
      // 先调用后端 API 停止流式生成，避免继续消耗 token
      const conversationId = currentConversationIdRef.current;
      const nodeId = currentNodeId;
      if (conversationId && nodeId) {
        try {
          await messageApi.stopStream(conversationId, nodeId);
        } catch (e) {
          console.error('Failed to stop stream:', e);
        }
      }
      abortControllerRef.current.abort();
    }
  }, [isStreaming, currentNodeId]);

  useEffect(() => {
    return () => {
      // if (isStreaming) {
      //   abortStreaming();
      // }
    };
  }, [isStreaming, abortStreaming]);

  const reset = useCallback(() => {
    setStreamedContent('');
    setError(null);
    setTokensUsed(0);
    setCurrentNodeId(null);
    setStreamingConversationId(null);
  }, []);

  return {
    isStreaming,
    streamedContent,
    currentNodeId,
    error,
    tokensUsed,
    streamingConversationId,
    startStreaming,
    abortStreaming,
    reset,
  };
};