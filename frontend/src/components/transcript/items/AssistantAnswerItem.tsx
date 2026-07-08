import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import MarkdownContent from '../../MarkdownContent';
import type { TranscriptCopyHandler, TranscriptItem } from '../../../types/transcript';
import { getItemText } from './itemText';
import { getStreamStatusText } from '../../../utils/generationStatus';

export function AssistantAnswerItem({ item, onCopy }: { item: TranscriptItem; onCopy?: TranscriptCopyHandler }) {
  const [copied, setCopied] = useState(false);
  const text = getItemText(item);
  const compactAfterProcess = item.props?.compact_after_process === true;
  const streamStatus = typeof item.props?.stream_status === 'string' ? item.props.stream_status : item.status;
  const streamErrorMessage = typeof item.props?.stream_error_message === 'string' ? item.props.stream_error_message : null;
  const statusLabel = getStreamStatusText(streamStatus || '', streamErrorMessage);
  if (!text) return null;

  const handleCopy = async () => {
    await onCopy?.(item, text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <div className={cn('chat-message-row w-full flex flex-col group items-start', compactAfterProcess ? 'mt-0 mb-2' : 'my-2')} role="listitem">
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
        {statusLabel && (
          <div className="mt-1 text-xs text-destructive">
            {statusLabel}
          </div>
        )}
      </div>
    </div>
  );
}
