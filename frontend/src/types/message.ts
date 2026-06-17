export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';

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
  parent_node_id?: string;
  parent_id?: string;
  model?: string;
  tokens_used?: number;
  timestamp: number;
  generation_info?: GenerationInfo | null;  // 生成信息（仅助手消息有）
  // 可扩展字段（未来工具调用/推理；当前文本路径不填写）
  name?: string;
  tool_calls?: any[];
  tool_call_id?: string;
  tool_results?: any[];
  reasoning?: string;
}

export interface SendMessageRequest {
  content: string;
  model_id?: string;
  reasoning_effort?: string | null;
  thinking_enabled?: boolean | null;
}

export type StreamStatus = 'start' | 'content' | 'complete' | 'error' | 'stopped';

export interface StreamChunk {
  status: StreamStatus;
  content: string | null;
  node_id: string | null;
  conversation_id: string | null;
  error?: string | null;
  tokens_used: number;
  // 可扩展字段（未来推理/工具事件；缺省按文本处理）
  event_type?: 'text' | 'reasoning' | 'tool_call' | 'tool_result';
  reasoning?: string | null;
  tool_call?: any;
}