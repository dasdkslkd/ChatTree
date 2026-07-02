export type RunKind = 'chat' | 'side_question' | 'subagent' | 'terminal' | 'workflow' | 'workflow_step' | 'direct_response';
export type RunStatus = 'queued' | 'running' | 'waiting_approval' | 'stopping' | 'completed' | 'failed' | 'cancelled';

export interface RunRecord {
  run_id: string;
  conversation_id: string;
  kind: RunKind;
  status: RunStatus;
  anchor_node_id?: string | null;
  target_node_id?: string | null;
  parent_run_id?: string | null;
  summary?: string;
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
  target_node_id?: string | null;
  content?: string | null;
  reasoning?: string | null;
  error?: string | null;
  payload?: unknown;
}
