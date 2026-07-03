import type { TaskRecord } from '../types/task';

const OPEN_STATUSES = new Set(['pending', 'in_progress', 'blocked']);
const STATUS_RANK: Record<string, number> = {
  blocked: 0,
  in_progress: 1,
  pending: 2,
  completed: 3,
  cancelled: 4,
};

export function isOpenTask(task: Pick<TaskRecord, 'status'>): boolean {
  return OPEN_STATUSES.has(String(task.status || ''));
}

export function taskStatusLabel(status: unknown): string {
  if (status === 'pending') return '待处理';
  if (status === 'in_progress') return '进行中';
  if (status === 'completed') return '已完成';
  if (status === 'blocked') return '已阻塞';
  if (status === 'cancelled') return '已取消';
  return typeof status === 'string' && status.trim() ? status.trim() : '未知';
}

export function taskOwnerLabel(ownerType: unknown): string {
  if (ownerType === 'subagent') return '后台分支';
  if (ownerType === 'workflow') return 'Workflow';
  if (ownerType === 'workflow_step') return 'Workflow 步骤';
  if (ownerType === 'command') return '后台命令';
  if (ownerType === 'assistant') return '主对话';
  return typeof ownerType === 'string' && ownerType.trim() ? ownerType.trim() : '任务';
}

function compactText(value: unknown, maxLength: number): string {
  const text = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  if (!text) return '';
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 3)).trimEnd()}...`;
}

export function compactTaskTitle(task: Pick<TaskRecord, 'title' | 'detail'>, maxLength = 120): string {
  return compactText(task.title || task.detail || '未命名任务', maxLength);
}

export function sortTasksForDisplay<T extends Pick<TaskRecord, 'status' | 'updated_at' | 'created_at'>>(tasks: T[]): T[] {
  return [...tasks].sort((a, b) => {
    const statusDelta = (STATUS_RANK[String(a.status)] ?? 99) - (STATUS_RANK[String(b.status)] ?? 99);
    if (statusDelta !== 0) return statusDelta;
    return (b.updated_at || b.created_at || 0) - (a.updated_at || a.created_at || 0);
  });
}
