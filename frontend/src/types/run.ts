export type RunKind = 'chat' | 'side_question' | 'subagent' | 'command' | 'workflow' | 'workflow_step' | 'direct_response';
export type RunStatus = 'queued' | 'running' | 'waiting_approval' | 'stopping' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'stopped';

export interface RunRecord {
  run_id: string;
  conversation_id: string;
  kind: RunKind;
  status: RunStatus;
  anchor_node_id?: string | null;
  target_node_id?: string | null;
  created_by_run_id?: string | null;
  cancellation_parent_run_id?: string | null;
  summary?: string;
  step?: number | null;
  event_count: number;
  metadata?: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  finished_at?: number | null;
}

export interface RunEventPayload {
  run_id: string;
  conversation_id?: string;
  kind?: RunKind | string;
  status?: string;
  event_type?: string;
  event_index?: number;
  created_by_run_id?: string | null;
  cancellation_parent_run_id?: string | null;
  target_node_id?: string | null;
  content?: string | null;
  reasoning?: string | null;
  error?: string | null;
  payload?: unknown;
}
