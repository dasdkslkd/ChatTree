export type MessageRole = 'user' | 'assistant' | 'system';

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  node_id: string;
  parent_id?: string;
  model?: string;
  tokens_used?: number;
  timestamp: number;
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