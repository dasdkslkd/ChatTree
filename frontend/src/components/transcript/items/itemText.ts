import type { TranscriptItem } from '../../../types/transcript';

export function getItemText(item: TranscriptItem, fallback = ''): string {
  const candidates = (() => {
    switch (item.type) {
      case 'user_message':
      case 'assistant_answer':
      case 'task_notification':
      case 'compact':
        return [item.content];
      case 'plan_approval':
        return [item.plan, item.feedback];
      case 'plan_question':
        return [...item.questions.map((entry) => entry.question), ...(item.answers ?? [])];
      case 'tool_approval':
        return [item.args_preview, item.result_preview];
      case 'assistant_process':
      case 'run_status':
        return [item.message];
      default:
        return [];
    }
  })();
  return candidates.find((value): value is string => typeof value === 'string' && value.trim().length > 0) || fallback;
}

export function getStringProp(item: TranscriptItem, key: string): string {
  const value = (item as unknown as Record<string, unknown>)[key];
  return typeof value === 'string' ? value : '';
}

export function getStatusText(item: TranscriptItem): string {
  return 'status' in item ? item.status : getStringProp(item, 'status');
}
