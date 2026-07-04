import { Copy } from 'lucide-react';
import MarkdownContent from '../../MarkdownContent';
import type { TranscriptCopyHandler, TranscriptItem } from '../../../types/transcript';
import { getItemText } from './itemText';

export function AssistantAnswerItem({ item, onCopy }: { item: TranscriptItem; onCopy?: TranscriptCopyHandler }) {
  const text = getItemText(item);
  if (!text) return null;

  return (
    <div className="transcript-assistant-answer w-full my-2 flex flex-col items-start" role="listitem">
      <div className="group flex max-w-full items-start gap-1">
        <div
          className="max-w-full prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
          style={{
            color: 'var(--fg-secondary)',
            fontSize: 'var(--codex-chat-font-size)',
            lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
          }}
        >
          <MarkdownContent enableMermaid>{text}</MarkdownContent>
        </div>
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
      </div>
    </div>
  );
}
