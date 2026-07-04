import type { TranscriptItem } from '../types/transcript';

const transcriptItemTypes = new Set([
  'compact_boundary',
  'compact_summary',
  'user_message',
  'assistant_process',
  'assistant_answer',
  'tool_group',
  'plan_card',
  'task_notification',
  'task_progress',
  'run_draft',
  'side_run_notification',
]);

function normalizeTranscriptItem(item: TranscriptItem): TranscriptItem {
  if (item.type) return item;
  const itemType = typeof item.item_type === 'string' && transcriptItemTypes.has(item.item_type)
    ? item.item_type
    : undefined;
  return itemType ? { ...item, type: itemType as TranscriptItem['type'] } : item;
}

export function normalizeTranscriptItems(items: TranscriptItem[]): TranscriptItem[] {
  return items
    .filter((item) => !item.visibility || item.visibility === 'main')
    .map(normalizeTranscriptItem);
}
