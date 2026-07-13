export type TranscriptItemType =
  | 'compact_boundary'
  | 'compact_summary'
  | 'user_message'
  | 'assistant_process'
  | 'assistant_answer'
  | 'tool_group'
  | 'plan_card'
  | 'task_notification'
  | 'task_progress'
  | 'run_draft'
  | 'side_run_notification';

export type TranscriptItemVisibility = 'main' | 'side_panel' | 'hidden';

export interface TranscriptItem {
  id: string;
  type: TranscriptItemType;
  item_type?: TranscriptItemType | string | null;
  conversation_id?: string | null;
  node_id?: string | null;
  anchor_node_id?: string | null;
  message_id?: string | null;
  run_id?: string | null;
  plan_id?: string | null;
  local_order?: number | null;
  status?: string | null;
  summary?: string | null;
  preview?: string | null;
  visibility?: TranscriptItemVisibility | null;
  props?: Record<string, unknown>;
  created_at?: number | string | null;
  updated_at?: number | string | null;
}

export interface TranscriptSnapshot {
  conversation_id: string;
  tip_node_id: string;
  revision: number;
  items: TranscriptItem[];
}

export type TranscriptPlanActionHandler = (item: TranscriptItem) => void | Promise<void>;
export type TranscriptPlanQuestionAnswerHandler = (item: TranscriptItem, answer: string) => void | Promise<void>;
export type TranscriptCopyHandler = (item: TranscriptItem, text: string) => void | Promise<void>;
export type TranscriptUserMessageActionHandler = (item: TranscriptItem, text: string) => void | Promise<void>;
export type TranscriptUserMessageDeleteHandler = (item: TranscriptItem) => void | Promise<void>;

export interface TranscriptActionHandlers {
  onApprovePlan?: TranscriptPlanActionHandler;
  onRejectPlan?: TranscriptPlanActionHandler;
  onAnswerPlanQuestion?: TranscriptPlanQuestionAnswerHandler;
  onCopyItem?: TranscriptCopyHandler;
  onEditUserMessage?: TranscriptUserMessageActionHandler;
  onDeleteUserMessage?: TranscriptUserMessageDeleteHandler;
  planActionPending?: string | null;
  planError?: string | null;
}
