import type { Message } from '../types/message';
import type { TranscriptItem } from '../types/transcript';

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
}

export interface TaskNotificationRecordLike {
  id: string;
  conversation_id: string;
  source_run_id: string;
  source_run_kind: string;
  status: string;
  delivery_node_id?: string | null;
  delivered_run_id?: string | null;
  delivered_node_id?: string | null;
  summary?: string | null;
  content?: string | null;
  payload?: Record<string, unknown> | null;
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
  };
}

export function createTaskNotificationTranscriptItem(
  notification: TaskNotificationRecordLike,
  options: {
    runId?: string | null;
    nodeId?: string | null;
  } = {},
): TranscriptItem {
  const payload = notification.payload || {};
  const summary = notification.summary || 'Task notification';
  const content = notification.content || '';
  const status = String(payload.source_status || notification.status || '');
  return {
    id: `live-task-notification-${options.runId || notification.delivered_run_id || notification.id}`,
    type: 'task_notification',
    conversation_id: notification.conversation_id,
    node_id: options.nodeId || notification.delivered_node_id || null,
    anchor_node_id: notification.delivery_node_id || null,
    run_id: options.runId || notification.delivered_run_id || null,
    status,
    summary,
    preview: summary || content || 'Task notification',
    visibility: 'main',
    props: {
      kind: 'task_notification',
      summary,
      source_run_id: notification.source_run_id,
      source_run_kind: notification.source_run_kind,
      ...payload,
      content,
    },
  };
}

export function hasTaskNotificationTranscriptItem(
  items: TranscriptItem[],
  notification: TaskNotificationRecordLike,
  nodeId?: string | null,
): boolean {
  return items.some((item) => {
    if (item.type !== 'task_notification' && item.item_type !== 'task_notification') return false;
    if (nodeId && item.node_id && item.node_id !== nodeId) return false;
    return item.props?.source_run_id === notification.source_run_id;
  });
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
