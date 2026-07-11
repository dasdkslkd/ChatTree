export type TaskStepStatus = 'pending' | 'blocked' | 'completed';
export type TaskStatus = 'pending' | 'blocked';
export type TaskExecutionState = 'idle' | 'running' | 'stopping';
export type TaskContextMode = 'attached' | 'detached';

export interface ActiveTaskStep {
  position: number;
  title: string;
  detail: string;
  status: TaskStepStatus;
  evidence_summary: string;
}

export interface ActiveTaskRecord {
  title: string;
  detail: string;
  status: TaskStatus;
  execution_state: TaskExecutionState;
  active_run_id?: string | null;
  active_step?: number | null;
  steps: ActiveTaskStep[];
}
