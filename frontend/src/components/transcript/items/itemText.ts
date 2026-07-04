import type { TranscriptItem } from '../../../types/transcript';

export function getItemText(item: TranscriptItem, fallback = ''): string {
  const candidates = [
    item.preview,
    item.summary,
    getStringProp(item, 'preview'),
    getStringProp(item, 'summary'),
    getStringProp(item, 'content'),
    getStringProp(item, 'title'),
  ];
  return candidates.find((value): value is string => typeof value === 'string' && value.trim().length > 0) || fallback;
}

export function getStringProp(item: TranscriptItem, key: string): string {
  const value = item.props?.[key];
  return typeof value === 'string' ? value : '';
}

export function getStatusText(item: TranscriptItem): string {
  return item.status || getStringProp(item, 'status');
}
