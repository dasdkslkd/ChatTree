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
  // 流式输出用时（毫秒）
  const [streamDuration, setStreamDuration] = useState<number>(0);
  // 流式输出状态信息
  const [streamStatus, setStreamStatus] = useState<'idle' | 'streaming' | 'completed' | 'error' | 'stopped'>('idle');

  const abortControllerRef = useRef<AbortController | null>(null);
  const currentConversationIdRef = useRef<string | null>(null);
  const startTimeRef = useRef<number>(0);
  const durationIntervalRef = useRef<number | null>(null);
  
  // 使用 ref 存储回调，避免闭包问题
  const optionsRef = useRef(options);
  optionsRef.current = options;

  // 开始计时
  const startTimer = useCallback(() => {
    startTimeRef.current = Date.now();
    setStreamDuration(0);
    setStreamStatus('streaming');
    // 使用 interval 更新持续时间
    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current);
    }
    durationIntervalRef.current = window.setInterval(() => {
      setStreamDuration(Date.now() - startTimeRef.current);
    }, 100);
  }, []);

  // 停止计时
  const stopTimer = useCallback((status: 'completed' | 'error' | 'stopped') => {
    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current);
      durationIntervalRef.current = null;
    }
    setStreamDuration(Date.now() - startTimeRef.current);
    setStreamStatus(status);
  }, []);

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
      
      // 开始计时
      startTimer();

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

          if (chunk.status === 'complete') {
            stopTimer('completed');
            break;
          } else if (chunk.status === 'stopped') {
            stopTimer('stopped');
            break;
          } else if (chunk.status === 'error') {
            throw new Error(chunk.error || 'Stream error occurred');
          }
        }

        setIsStreaming(false);
        optionsRef.current.onComplete?.(fullContent, conversationId);
      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') {
          stopTimer('stopped');
          setIsStreaming(false);
          return;
        }
        setError(err as Error);
        stopTimer('error');
        setIsStreaming(false);
        optionsRef.current.onError?.(err as Error, conversationId);
      }
    },
    [startTimer, stopTimer] // 添加依赖
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
      stopTimer('stopped');
    }
  }, [isStreaming, currentNodeId, stopTimer]);

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
    setStreamDuration(0);
    setStreamStatus('idle');
    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current);
      durationIntervalRef.current = null;
    }
  }, []);

  return {
    isStreaming,
    streamedContent,
    currentNodeId,
    error,
    tokensUsed,
    streamingConversationId,
    streamDuration,
    streamStatus,
    startStreaming,
    abortStreaming,
    reset,
  };
};