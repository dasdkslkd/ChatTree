import type { TranscriptItem } from '../types/transcript';

export function normalizeTranscriptItems(items: TranscriptItem[]): TranscriptItem[] {
  return items.filter((item) => !item.visibility || item.visibility === 'main');
}
