import { apiClient } from './client';
import type { Message, SendMessageRequest, StreamChunk } from '../types/message';

export const messageApi = {
  // 发送消息（非流式）
  send: async (
    conversationId: string,
    data: SendMessageRequest
  ): Promise<{ message: string; conversation_id: string; node_id: string }> => {
    const response = await apiClient.post(`/conversations/${conversationId}/messages`, data);
    return response.data;
  },

  // 流式发送消息
  stream: async function* (
    conversationId: string,
    data: SendMessageRequest
  ): AsyncGenerator<StreamChunk, void> {
    const response = await fetch(`/api/conversations/${conversationId}/messages/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(data),
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const reader = response.body?.getReader();
    const decoder = new TextDecoder();

    if (!reader) {
      throw new Error('Response body is not readable');
    }

    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      
      // 按双换行符分割 SSE 消息
      const parts = buffer.split('\n\n');
      // 最后一部分可能不完整，保留到下次处理
      buffer = parts.pop() || '';

      for (const part of parts) {
        const trimmed = part.trim();
        if (!trimmed) continue;
        
        if (trimmed.startsWith('data: ')) {
          const jsonData = trimmed.slice(6);
          if (jsonData === '[DONE]') {
            return;
          }
          try {
            const parsed: StreamChunk = JSON.parse(jsonData);
            yield parsed;
          } catch (e) {
            console.error('Failed to parse stream chunk:', e, jsonData);
          }
        }
      }
    }
    
    // 处理剩余的buffer
    if (buffer.trim()) {
      const trimmed = buffer.trim();
      if (trimmed.startsWith('data: ')) {
        const jsonData = trimmed.slice(6);
        if (jsonData !== '[DONE]') {
          try {
            const parsed: StreamChunk = JSON.parse(jsonData);
            yield parsed;
          } catch (e) {
            console.error('Failed to parse final stream chunk:', e, jsonData);
          }
        }
      }
    }
  },

  // 获取消息历史
  getHistory: async (conversationId: string): Promise<Message[]> => {
    const response = await apiClient.get(`/conversations/${conversationId}/messages`);
    return response.data;
  },
};