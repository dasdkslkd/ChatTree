export type MessageRole = 'user' | 'notify' | 'assistant' | 'system' | 'tool';

export interface GenerationInfo {
  duration_ms: number;  // 生成用时（毫秒）
  status: 'completed' | 'error' | 'stopped';  // 生成状态
  error_message?: string | null;  // 错误信息
  tokens_used?: number;  // 使用的token数
  usage_info?: UsageInfo | null;
}

export type TaskContextMode = 'attached' | 'detached';

export interface UsageInfo {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  cached_tokens?: number;
  reasoning_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
  source?: string;
  raw?: Record<string, unknown>;
}

export interface NodeUsage {
  turn_usage?: UsageInfo;
  branch_usage?: UsageInfo;
  active_context_usage?: UsageInfo;
  model_context_window?: number | null;
}

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  subtype?: string | null;
  node_id: string;
  parent_node_id?: string;
  parent_id?: string;
  model?: string;
  tokens_used?: number;
  branch_total_tokens?: number;
  branch_usage_info?: UsageInfo | null;
  context_usage?: NodeUsage | null;
  timestamp: number;
  generation_info?: GenerationInfo | null;  // 生成信息（仅助手消息有）
  name?: string;
  tool_calls?: any[];
  tool_call_id?: string;
  reasoning?: string;
  compact_metadata?: {
    trigger?: 'manual' | 'auto' | string;
    pre_tokens?: number;
    messages_to_keep?: number;
    last_pre_compact_message_id?: string;
    restored_files?: Array<{
      filename: string;
      content?: string;
      truncated?: boolean;
    }>;
  } | null;
  is_visible_in_transcript_only?: boolean;
  import_files?: Array<{
    filename: string;
  }>;
  image_refs?: Array<{
    filename: string;
    mime_type?: string;
  }>;
  tool_permission_mode?: ToolPermissionMode | null;
  task_context_mode?: TaskContextMode | null;
}

export interface SendMessageRequest {
  content: string;
  parent_node_id: string;
  focus_new_node?: boolean;
  model_id?: string;
  provider_id?: string;
  reasoning_effort?: string | null;
  thinking_enabled?: boolean | null;
  tool_permission_mode?: ToolPermissionMode;
  task_context_mode?: TaskContextMode;
  import_files?: Array<{
    filename: string;
  }>;
  image_refs?: Array<{
    filename: string;
    mime_type?: string;
  }>;
}

export type ToolPermissionMode = 'auto_approve' | 'modify_only' | 'ask_always' | 'plan';
