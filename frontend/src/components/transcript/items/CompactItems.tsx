import { Archive } from 'lucide-react';
import MarkdownContent from '../../MarkdownContent';
import type { TranscriptItem } from '../../../types/transcript';
import { getItemText } from './itemText';

export function CompactItem({ item }: { item: TranscriptItem }) {
  const text = getItemText(item, item.type === 'compact_boundary' ? 'Context compacted' : 'Compact summary');

  return (
    <div className="transcript-compact-item w-full my-2 flex flex-col items-center" role="listitem">
      <div
        className="flex max-w-[760px] min-w-0 flex-col gap-1 rounded-md px-3 py-2 text-xs"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-button-tertiary-hover)',
          color: 'var(--fg-tertiary)',
        }}
      >
        <div className="flex items-center gap-2 font-medium">
          <Archive className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <span>{item.type === 'compact_boundary' ? 'Compact boundary' : 'Compact summary'}</span>
        </div>
        {text && (
          <div className="prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2">
            <MarkdownContent>{text}</MarkdownContent>
          </div>
        )}
      </div>
    </div>
  );
}
