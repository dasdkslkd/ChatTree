import { ClipboardList } from 'lucide-react';
import MarkdownContent from '../../MarkdownContent';
import type { TranscriptItem } from '../../../types/transcript';
import { getItemText, getStatusText } from './itemText';

export function PlanCardItem({ item }: { item: TranscriptItem }) {
  const text = getItemText(item, 'Plan update');
  const status = getStatusText(item);

  return (
    <div className="transcript-plan-card w-full my-2 flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] w-full min-w-0 flex-col gap-2 rounded-md px-3 py-3 text-sm"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-secondary)',
          color: 'var(--fg-secondary)',
        }}
      >
        <div className="flex items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
          <ClipboardList className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <span>Plan{status ? ` · ${status}` : ''}</span>
        </div>
        <div
          className="min-w-0 rounded-sm px-2.5 py-2 prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
          style={{
            border: '0.5px solid var(--border)',
            background: 'var(--bg-input)',
            color: 'var(--fg-secondary)',
            fontSize: 'var(--codex-chat-font-size)',
            lineHeight: 'calc(var(--codex-chat-font-size) + 8px)',
          }}
        >
          <MarkdownContent enableMermaid>{text}</MarkdownContent>
        </div>
      </div>
    </div>
  );
}
