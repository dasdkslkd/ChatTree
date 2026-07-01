import type { Message } from '../types/message';

type MessageLike = Pick<Message, 'role' | 'subtype'>;

export function isTaskNotificationMessage(message: MessageLike): boolean {
  return message.role === 'user' && message.subtype === 'task_notification';
}

export function shouldExportMessage(message: MessageLike): boolean {
  return !isTaskNotificationMessage(message);
}

export function getTreeUserContent(node: {
  user_content?: string | null;
  user_subtype?: string | null;
}): string {
  return node.user_subtype === 'task_notification' ? '' : (node.user_content ?? '');
}
