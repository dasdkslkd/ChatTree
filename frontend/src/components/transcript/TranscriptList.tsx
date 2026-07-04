import type { TranscriptItem } from '../../types/transcript';
import { normalizeTranscriptItems } from '../../utils/transcriptItems';
import { TranscriptItemRenderer } from './TranscriptItemRenderer';

export function TranscriptList({ items }: { items: TranscriptItem[] }) {
  const normalizedItems = normalizeTranscriptItems(items);

  if (normalizedItems.length === 0) return null;

  return (
    <div className="transcript-list flex w-full flex-col" role="list">
      {normalizedItems.map((item) => (
        <TranscriptItemRenderer key={item.id} item={item} />
      ))}
    </div>
  );
}
