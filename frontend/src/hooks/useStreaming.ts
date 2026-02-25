import { useState, useCallback, useEffect, useRef } from 'react';
import type { StreamChunk, SendMessageRequest } from '../types/message';
import { messageApi } from '../api/message';

interface UseStreamingOptions {
  onChunk?: (chunk: StreamChunk) => void;
  onComplete?: (fullContent: string) => void;
  onError?: (error: Error) => void;
}

export const useStreaming = (options: UseStreamingOptions = {}) => {
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamedContent, setStreamedContent] = useState('');
  const [currentNodeId, setCurrentNodeId] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [tokensUsed, setTokensUsed] = useState(0);

  const abortControllerRef = useRef<AbortController | null>(null);
  
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
          optionsRef.current.onChunk?.(chunk);

          if (chunk.status === 'complete' || chunk.status === 'stopped') {
            break;
          } else if (chunk.status === 'error') {
            throw new Error(chunk.error || 'Stream error occurred');
          }
        }

        setIsStreaming(false);
        optionsRef.current.onComplete?.(fullContent);
      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') {
          setIsStreaming(false);
          return;
        }
        setError(err as Error);
        setIsStreaming(false);
        optionsRef.current.onError?.(err as Error);
      }
    },
    [] // 空依赖数组，因为使用了 ref
  );

  const abortStreaming = useCallback(() => {
    if (abortControllerRef.current && isStreaming) {
      abortControllerRef.current.abort();
    }
  }, [isStreaming]);

  useEffect(() => {
    return () => {
      if (isStreaming) {
        abortStreaming();
      }
    };
  }, [isStreaming, abortStreaming]);

  return {
    isStreaming,
    streamedContent,
    currentNodeId,
    error,
    tokensUsed,
    startStreaming,
    abortStreaming,
    reset: () => {
      setStreamedContent('');
      setError(null);
      setTokensUsed(0);
      setCurrentNodeId(null);
    },
  };
};