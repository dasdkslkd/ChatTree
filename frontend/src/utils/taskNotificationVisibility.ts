import type { Message } from '../types/message';

type MessageLike = Pick<Message, 'role' | 'subtype'> & {
  content?: unknown;
  metadata?: {
    message_kind?: unknown;
    display?: unknown;
  } | null;
};

export interface TaskNotificationSummary {
  title: string;
  detail: string;
  command: string;
  output: string;
  status: string;
  kind: string;
  taskId: string;
}

function containsTaskNotificationTag(value: unknown): boolean {
  return typeof value === 'string' && value.includes('<task-notification>');
}

function extractTaskNotificationJson(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string') return null;
  const match = value.match(/<task-notification>\s*([\s\S]*?)\s*<\/task-notification>/);
  if (!match) return null;
  try {
    const parsed = JSON.parse(match[1]);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function compactText(value: unknown, maxLength = 140): string {
  if (typeof value !== 'string') return '';
  const compact = value.replace(/\s+/g, ' ').trim();
  if (compact.length <= maxLength) return compact;
  return `${compact.slice(0, Math.max(0, maxLength - 1)).trimEnd()}...`;
}

function parseJsonObject(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function taskKindLabel(kind: unknown): string {
  if (kind === 'command') return '后台命令';
  if (kind === 'subagent') return '后台分支';
  if (kind === 'workflow') return 'Workflow';
  if (kind === 'workflow_step') return 'Workflow 步骤';
  return '后台任务';
}

function taskStatusLabel(status: unknown): string {
  if (status === 'completed') return '已完成';
  if (status === 'failed') return '失败';
  if (status === 'cancelled' || status === 'stopped') return '已取消';
  if (typeof status === 'string' && status.trim()) return status.trim();
  return '';
}

export function isTaskNotificationMessage(message: MessageLike): boolean {
  return (
    ((message.role === 'user' || message.role === 'notify') && message.subtype === 'task_notification')
    || message.metadata?.message_kind === 'task_notification'
    || message.metadata?.display === 'hidden'
    || containsTaskNotificationTag(message.content)
  );
}

export function isRenderableTaskNotificationMessage(message: MessageLike): boolean {
  return (
    ((message.role === 'user' || message.role === 'notify') && message.subtype === 'task_notification')
    || message.metadata?.message_kind === 'task_notification'
    || containsTaskNotificationTag(message.content)
  );
}

export function getTaskNotificationSummary(message: MessageLike): TaskNotificationSummary {
  const payload = extractTaskNotificationJson(message.content) || {};
  const embedded = parseJsonObject(payload.content) || {};
  const kind = payload.source_run_kind || payload.kind;
  const status = payload.source_status;
  const taskId = typeof payload.task_id === 'string' ? payload.task_id : '';
  const kindLabel = taskKindLabel(kind);
  const statusLabel = taskStatusLabel(status);
  const command = compactText(
    embedded.command
      || payload.original_slash_input
      || payload.delegated_task,
    120,
  );
  const output = compactText(
    embedded.stderr_tail
      || embedded.stdout_tail
      || embedded.error
      || payload.summary
      || (Object.keys(embedded).length > 0 ? '' : payload.content),
    160,
  );
  const detail = compactText(
    command
      || payload.original_slash_input
      || payload.delegated_task
      || payload.summary
      || payload.content
      || message.content,
  );
  return {
    title: statusLabel ? `${kindLabel} ${statusLabel}` : `${kindLabel}通知`,
    detail,
    command,
    output,
    status: statusLabel,
    kind: kindLabel,
    taskId,
  };
}

export function shouldExportMessage(message: MessageLike): boolean {
  return !isTaskNotificationMessage(message);
}

export function getTreeUserContent(node: {
  user_content?: string | null;
  user_subtype?: string | null;
  metadata?: {
    message_kind?: unknown;
    display?: unknown;
  } | null;
}): string {
  if (
    node.user_subtype === 'task_notification'
    || node.metadata?.message_kind === 'task_notification'
    || node.metadata?.display === 'hidden'
    || containsTaskNotificationTag(node.user_content)
  ) {
    return '';
  }
  return node.user_content ?? '';
}
