import { BellRing } from 'lucide-react';
import type { TranscriptItem } from '../../../types/transcript';
import { getItemText, getStatusText } from './itemText';

export function TaskNotificationItem({ item }: { item: TranscriptItem }) {
  const text = getItemText(item, 'Task update');
  const status = getStatusText(item);

  return (
    <div className="transcript-task-notification w-full my-1 flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] min-w-0 items-center gap-2 rounded-md px-2.5 py-1 text-xs"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-button-tertiary-hover)',
          color: 'var(--fg-tertiary)',
        }}
      >
        <BellRing className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
        {status && (
          <span
            className="shrink-0 rounded-sm px-1.5 py-0.5 font-medium"
            style={{ color: 'var(--fg-secondary)', background: 'var(--bg-secondary)' }}
          >
            {status}
          </span>
        )}
        <span className="min-w-[120px] truncate" style={{ color: 'var(--fg-secondary)' }}>
          {text}
        </span>
      </div>
    </div>
  );
}
