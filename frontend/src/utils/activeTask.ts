import type { ActiveTaskRecord, ActiveTaskStep } from '../types/task';

export function taskStatusLabel(status: unknown): string {
  if (status === 'pending') return '待处理';
  if (status === 'running') return '运行中';
  if (status === 'stopping') return '停止中';
  if (status === 'completed') return '已完成';
  if (status === 'blocked') return '已阻塞';
  return typeof status === 'string' && status.trim() ? status.trim() : '未知';
}

function compactText(value: unknown, maxLength: number): string {
  const text = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  if (!text || text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 3)).trimEnd()}...`;
}

export function compactTaskTitle(
  task: Pick<ActiveTaskRecord, 'title' | 'detail'>,
  maxLength = 120,
): string {
  return compactText(task.title || task.detail || '未命名任务', maxLength);
}

export type TaskPanelStepItem = {
  step: ActiveTaskStep;
  title: string;
  statusLabel: string;
  running: boolean;
};

export type TaskPanelItem = {
  task: ActiveTaskRecord;
  title: string;
  statusLabel: string;
  progressText: string;
  running: boolean;
  stopping: boolean;
  steps: TaskPanelStepItem[];
};

export function shouldPollTaskState(options: {
  conversationId?: string | null;
  activeRunCount?: number;
  activeTask?: ActiveTaskRecord | null;
  visibleNotificationCount?: number;
}): boolean {
  if (!options.conversationId) return false;
  return Boolean(options.activeTask)
    || Number(options.activeRunCount || 0) > 0
    || Number(options.visibleNotificationCount || 0) > 0;
}

export function createTaskPanelItem(task: ActiveTaskRecord | null): TaskPanelItem | null {
  if (!task) return null;
  const completed = task.steps.filter((step) => step.status === 'completed').length;
  const running = task.execution_state === 'running';
  const stopping = task.execution_state === 'stopping';
  return {
    task,
    title: compactTaskTitle(task),
    statusLabel: taskStatusLabel(stopping ? 'stopping' : running ? 'running' : task.status),
    progressText: `${completed}/${task.steps.length} 步`,
    running,
    stopping,
    steps: task.steps.map((step) => ({
      step,
      title: compactText(step.title || step.detail || `步骤 ${step.position}`, 100),
      statusLabel: taskStatusLabel(step.status),
      running: running && task.active_step === step.position,
    })),
  };
}
