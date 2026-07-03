export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'blocked' | 'cancelled';
export type TaskOwnerType = 'assistant' | 'subagent' | 'workflow' | 'command';

export interface TaskRecord {
  task_id: string;
  conversation_id: string;
  title: string;
  detail: string;
  status: TaskStatus | string;
  owner_type: TaskOwnerType | string;
  owner_run_id?: string | null;
  created_by_run_id?: string | null;
  evidence_run_id?: string | null;
  evidence_summary: string;
  metadata?: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  finished_at?: number | null;
}

export interface CreateTaskRequest {
  title: string;
  detail?: string;
}

export interface UpdateTaskRequest {
  title?: string;
  detail?: string;
  status?: TaskStatus | string;
  evidence_summary?: string;
}
