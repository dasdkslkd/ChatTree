import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { Button } from '@/components/ui/button';
import MarkdownContent from '../../MarkdownContent';
import type { TranscriptCopyHandler, TranscriptItem } from '../../../types/transcript';
import { getItemText } from './itemText';

export function AssistantAnswerItem({ item, onCopy }: { item: TranscriptItem; onCopy?: TranscriptCopyHandler }) {
  const [copied, setCopied] = useState(false);
  const text = getItemText(item);
  if (!text) return null;

  const handleCopy = async () => {
    await onCopy?.(item, text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <div className="chat-message-row w-full my-2 flex flex-col group items-start" role="listitem">
      <div className="flex flex-col max-w-full items-start w-full">
        <div
          className="max-w-full w-fit px-3 py-2 rounded-2xl leading-relaxed prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
          style={{
            color: 'var(--fg-secondary)',
            fontSize: 'var(--codex-chat-font-size)',
            lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
          }}
        >
          <MarkdownContent enableMermaid>{text}</MarkdownContent>
        </div>
        {onCopy && (
          <div className="flex items-center gap-1 mt-0.5 self-start justify-start">
            <Button
              variant="ghost"
              size="sm"
              className="opacity-0 group-hover:opacity-100 transition-opacity p-0 h-5 w-5"
              onClick={handleCopy}
              aria-label="复制消息"
            >
              {copied ? <Check className="h-3.5 w-3.5 text-green-500" /> : <Copy className="h-3.5 w-3.5" />}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
