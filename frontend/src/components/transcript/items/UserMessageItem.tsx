import MarkdownContent from '../../MarkdownContent';
import type { TranscriptItem } from '../../../types/transcript';
import { getItemText } from './itemText';

export function UserMessageItem({ item }: { item: TranscriptItem }) {
  const text = getItemText(item);
  if (!text) return null;

  return (
    <div className="transcript-user-message w-full my-2 flex flex-col items-end" role="listitem">
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
  );
}
