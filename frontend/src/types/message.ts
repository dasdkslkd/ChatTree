export type MessageRole = 'user' | 'notify' | 'assistant' | 'system' | 'tool';

export interface GenerationInfo {
  duration_ms: number;  // 生成用时（毫秒）
  status: 'completed' | 'error' | 'stopped';  // 生成状态
  error_message?: string | null;  // 错误信息
  tokens_used?: number;  // 使用的token数
  usage_info?: UsageInfo | null;
  task_guard?: {
    open_task_count?: number;
    nudged?: boolean;
  } | null;
}

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
  // 可扩展字段（未来工具调用/推理；当前文本路径不填写）
  name?: string;
  tool_calls?: any[];
  tool_call_id?: string;
  tool_results?: any[];
  tool_interactions?: any[];
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
  is_compact_summary?: boolean;
  is_visible_in_transcript_only?: boolean;
  import_files?: Array<{
    filename: string;
  }>;
  image_refs?: Array<{
    filename: string;
    mime_type?: string;
  }>;
  tool_permission_mode?: ToolPermissionMode | null;
}

export interface SendMessageRequest {
  content: string;
  parent_node_id?: string | null;
  focus_new_node?: boolean;
  model_id?: string;
  provider_id?: string;
  reasoning_effort?: string | null;
  thinking_enabled?: boolean | null;
  tool_permission_mode?: ToolPermissionMode;
  import_files?: Array<{
    filename: string;
  }>;
  image_refs?: Array<{
    filename: string;
    mime_type?: string;
  }>;
}

export type ToolPermissionMode = 'auto_approve' | 'modify_only' | 'ask_always' | 'plan';

export type StreamStatus = 'start' | 'content' | 'complete' | 'error' | 'stopped';

export type ToolApprovalStatus = 'pending' | 'approved' | 'denied' | 'expired' | 'cancelled';
export type ToolApprovalDecision = 'approve' | 'deny';
export type ToolApprovalScope = 'once' | 'session';

export interface ToolApprovalPayload {
  id: string;
  conversation_id?: string;
  node_id?: string;
  tool_call_id?: string;
  tool_name?: string;
  run_id?: string | null;
  run_kind?: string | null;
  created_by_run_id?: string | null;
  cancellation_parent_run_id?: string | null;
  root_run_id?: string | null;
  agent_name?: string | null;
  task_summary?: string | null;
  source_label?: string | null;
  arguments_preview?: string;
  risk?: string;
  risk_level?: string;
  reason?: string;
  suggested_actions?: string[];
  created_at?: number;
  expires_at?: number | null;
  status?: ToolApprovalStatus;
  grant_scope?: ToolApprovalScope | null;
}

export interface StreamChunk {
  status: StreamStatus;
  content: string | null;
  node_id: string | null;
  anchor_node_id?: string | null;
  target_node_id?: string | null;
  run_id?: string | null;
  event_index?: number | null;
  conversation_id: string | null;
  error?: string | null;
  tokens_used: number;
  // 可扩展字段（未来推理/工具事件；缺省按文本处理）
  event_type?: 'text' | 'reasoning' | 'process_content' | 'tool_call_start' | 'tool_call' | 'tool_result' | 'tool_approval_request' | 'tool_approval_result';
  reasoning?: string | null;
  tool_call?: any;
  tool_calls?: any[];
  approval?: ToolApprovalPayload;
}
