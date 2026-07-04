import { Loader2 } from 'lucide-react';
import type { TranscriptItem } from '../../../types/transcript';
import { getItemText, getStatusText } from './itemText';

export function RunDraftItem({ item }: { item: TranscriptItem }) {
  const text = getItemText(item, 'Run in progress');
  const status = getStatusText(item);
  const isActive = status === 'running' || status === 'streaming' || status === 'waiting_approval';

  return (
    <div className="transcript-run-draft w-full my-1 flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] min-w-0 items-center gap-2 rounded-md px-2.5 py-1 text-xs"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-button-tertiary-hover)',
          color: 'var(--fg-tertiary)',
        }}
      >
        <Loader2
          className={`h-3.5 w-3.5 shrink-0 ${isActive ? 'animate-spin' : ''}`}
          style={{ color: 'var(--icon-accent)' }}
        />
        {status && <span className="shrink-0 font-medium">{status}</span>}
        <span className="min-w-0 truncate">{text}</span>
      </div>
    </div>
  );
}
