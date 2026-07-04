import MarkdownContent from '../../MarkdownContent';
import type { TranscriptItem } from '../../../types/transcript';
import { getItemText } from './itemText';

export function AssistantAnswerItem({ item }: { item: TranscriptItem }) {
  const text = getItemText(item);
  if (!text) return null;

  return (
    <div className="transcript-assistant-answer w-full my-2 flex flex-col items-start" role="listitem">
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
    </div>
  );
}
