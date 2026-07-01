import type { Message } from '../types/message';

type MessageLike = Pick<Message, 'role' | 'subtype'> & {
  content?: unknown;
  metadata?: {
    message_kind?: unknown;
    display?: unknown;
  } | null;
};

function containsTaskNotificationTag(value: unknown): boolean {
  return typeof value === 'string' && value.includes('<task-notification>');
}

export function isTaskNotificationMessage(message: MessageLike): boolean {
  return (
    (message.role === 'user' && message.subtype === 'task_notification')
    || message.metadata?.message_kind === 'task_notification'
    || message.metadata?.display === 'hidden'
    || containsTaskNotificationTag(message.content)
  );
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
