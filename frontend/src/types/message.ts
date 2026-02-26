export type MessageRole = 'user' | 'assistant' | 'system';

export interface GenerationInfo {
  duration_ms: number;  // 生成用时（毫秒）
  status: 'completed' | 'error' | 'stopped';  // 生成状态
  error_message?: string | null;  // 错误信息
  tokens_used?: number;  // 使用的token数
}

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  node_id: string;
  parent_id?: string;
  model?: string;
  tokens_used?: number;
  timestamp: number;
  generation_info?: GenerationInfo | null;  // 生成信息（仅助手消息有）
}

export interface SendMessageRequest {
  content: string;
  model_id?: string;
}

export type StreamStatus = 'start' | 'content' | 'complete' | 'error' | 'stopped';

export interface StreamChunk {
  status: StreamStatus;
  content: string | null;
  node_id: string | null;
  conversation_id: string | null;
  error?: string | null;
  tokens_used: number;
}