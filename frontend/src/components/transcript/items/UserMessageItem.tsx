import { Copy } from 'lucide-react';
import MarkdownContent from '../../MarkdownContent';
import type { TranscriptCopyHandler, TranscriptItem } from '../../../types/transcript';
import { getItemText } from './itemText';

export function UserMessageItem({ item, onCopy }: { item: TranscriptItem; onCopy?: TranscriptCopyHandler }) {
  const text = getItemText(item);
  if (!text) return null;

  return (
    <div className="transcript-user-message w-full my-2 flex flex-col items-end" role="listitem">
      <div className="group flex max-w-full items-start gap-1">
        {onCopy && (
          <button
            type="button"
            className="mt-1 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
            style={{
              borderColor: 'var(--border)',
              background: 'var(--bg-button-tertiary)',
              color: 'var(--fg-tertiary)',
            }}
            aria-label="复制消息"
            onClick={() => onCopy(item, text)}
          >
            <Copy className="h-3.5 w-3.5" />
          </button>
        )}
        <div
          className="max-w-full w-fit rounded-2xl rounded-br-sm px-3 py-2 leading-relaxed prose prose-sm prose-invert max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
          style={{
            background: 'linear-gradient(160deg, rgba(217,119,87,0.16), rgba(217,119,87,0.08))',
            border: '0.5px solid rgba(217,119,87,0.28)',
            boxShadow: 'var(--highlight-top)',
            color: 'var(--fg-85)',
            fontSize: 'var(--codex-chat-font-size)',
            lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
          }}
        >
          <MarkdownContent enableMermaid>{text}</MarkdownContent>
        </div>
      </div>
    </div>
  );
}
