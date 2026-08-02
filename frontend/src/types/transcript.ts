export type TranscriptItemType =
  | 'user_message'
  | 'assistant_process'
  | 'assistant_answer'
  | 'plan_question'
  | 'plan_approval'
  | 'tool_approval'
  | 'task_notification'
  | 'compact'
  | 'run_status';

type NullableId = string | null;

interface TranscriptItemBase {
  id: string;
  type: TranscriptItemType;
  conversation_id: string;
}

export interface UserMessageItem extends TranscriptItemBase {
  type: 'user_message';
  node_id: string;
  parent_node_id: string | null;
  message_id: string;
  content: string;
  import_files: Array<{ filename: string }>;
  image_refs: Array<{ filename: string; mime_type?: string | null }>;
  tool_permission_mode?: 'auto_approve' | 'modify_only' | 'ask_always' | 'plan' | null;
  task_context_mode?: 'attached' | 'detached' | null;
  created_at: number;
}

export type AssistantProcessBlock =
  | { type: 'reasoning'; id: string; content: string; streaming: boolean }
  | { type: 'content'; id: string; content: string; streaming: boolean }
  | {
      type: 'tool_call';
      id: string;
      tool_call_id: string;
      tool_name: string;
      args_preview: string;
      result_preview: string | null;
      status: 'running' | 'complete' | 'error';
    };

export interface AssistantProcessItem extends TranscriptItemBase {
  type: 'assistant_process';
  node_id: string;
  run_id: NullableId;
  status: 'running' | 'complete' | 'stopped' | 'error';
  duration_ms: number | null;
  blocks: AssistantProcessBlock[];
  message?: string | null;
}

export interface AssistantAnswerItem extends TranscriptItemBase {
  type: 'assistant_answer';
  node_id: string;
  message_id: string;
  content: string;
  status: 'complete' | 'stopped' | 'error';
  finish_reason?: string | null;
}

export interface PlanQuestionOption {
  label?: string | null;
  description?: string | null;
}

export interface PlanQuestionItem extends TranscriptItemBase {
  type: 'plan_question';
  node_id: string;
  run_id: NullableId;
  plan_id: string;
  tool_call_id: string;
  status: 'awaiting_answer' | 'answered';
  question: string;
  options: PlanQuestionOption[];
  answer: string | null;
}

export interface PlanApprovalItem extends TranscriptItemBase {
  type: 'plan_approval';
  node_id: string;
  run_id: NullableId;
  plan_id: string;
  tool_call_id: string;
  status: 'awaiting_approval' | 'approved' | 'rejected';
  plan: string;
  feedback: string | null;
}

export interface ToolApprovalItem extends TranscriptItemBase {
  type: 'tool_approval';
  node_id: string;
  run_id: NullableId;
  tool_call_id: string;
  tool_name: string;
  status: 'awaiting_approval' | 'approved' | 'rejected';
  args_preview: string;
  result_preview: string | null;
}

export interface TaskNotificationItem extends TranscriptItemBase {
  type: 'task_notification';
  node_id: NullableId;
  notification_id: string;
  source_run_id: string;
  source_run_kind: string;
  status: 'unbound' | 'bound' | 'delivering' | 'delivered' | 'delivery_failed' | 'delivery_cancelled';
  summary: string;
  content: string;
  delivery_run_id: NullableId;
}

export interface CompactItem extends TranscriptItemBase {
  type: 'compact';
  node_id: string;
  summary_message_id: NullableId;
  content: string;
  trigger: 'manual' | 'auto';
  pre_tokens: number | null;
  messages_to_keep: number | null;
  created_at: number;
}

export interface RunStatusItem extends TranscriptItemBase {
  type: 'run_status';
  node_id: string;
  run_id: NullableId;
  status: 'running' | 'stopping' | 'stopped' | 'error' | 'reconnecting';
  message?: string | null;
}

export type TranscriptItem =
  | UserMessageItem
  | AssistantProcessItem
  | AssistantAnswerItem
  | PlanQuestionItem
  | PlanApprovalItem
  | ToolApprovalItem
  | TaskNotificationItem
  | CompactItem
  | RunStatusItem;

export type TranscriptPatchOperation =
  | { op: 'upsert'; item: TranscriptItem; index: number }
  | { op: 'remove'; id: string };

export interface TranscriptSnapshot {
  conversation_id: string;
  node_id: string | null;
  revision: number;
  items: TranscriptItem[];
}

export interface TranscriptPatch {
  type: 'transcript_patch';
  conversation_id: string;
  node_id: string;
  revision: number;
  operations: TranscriptPatchOperation[];
}

export type TranscriptPlanActionHandler = (item: PlanApprovalItem) => void | Promise<void>;
export type TranscriptPlanQuestionAnswerHandler = (item: PlanQuestionItem, answer: string) => void | Promise<void>;
export type TranscriptToolApprovalActionHandler = (item: ToolApprovalItem) => void | Promise<void>;
export type TranscriptCopyHandler = (item: TranscriptItem, text: string) => void | Promise<void>;
export type TranscriptUserMessageActionHandler = (item: UserMessageItem, text: string) => void | Promise<void>;
export type TranscriptUserMessageDeleteHandler = (item: UserMessageItem) => void | Promise<void>;

export interface TranscriptActionHandlers {
  onApprovePlan?: TranscriptPlanActionHandler;
  onRejectPlan?: TranscriptPlanActionHandler;
  onAnswerPlanQuestion?: TranscriptPlanQuestionAnswerHandler;
  onApproveTool?: TranscriptToolApprovalActionHandler;
  onRejectTool?: TranscriptToolApprovalActionHandler;
  onCopyItem?: TranscriptCopyHandler;
  onEditUserMessage?: TranscriptUserMessageActionHandler;
  onDeleteUserMessage?: TranscriptUserMessageDeleteHandler;
  planActionPending?: string | null;
  planError?: string | null;
  toolApprovalPending?: string | null;
  toolApprovalError?: string | null;
}
