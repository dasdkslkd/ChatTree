import { useState } from 'react';
import { Check, Copy, Pencil, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { TextTooltip } from '@/components/ui/text-tooltip';
import MarkdownContent from '../../MarkdownContent';
import type {
  TranscriptCopyHandler,
  UserMessageItem as UserMessageTranscriptItem,
  TranscriptUserMessageActionHandler,
  TranscriptUserMessageDeleteHandler,
} from '../../../types/transcript';
import { getItemText } from './itemText';

export function UserMessageItem({
  item,
  onCopy,
  onEdit,
  onDelete,
}: {
  item: UserMessageTranscriptItem;
  onCopy?: TranscriptCopyHandler;
  onEdit?: TranscriptUserMessageActionHandler;
  onDelete?: TranscriptUserMessageDeleteHandler;
}) {
  const [copied, setCopied] = useState(false);
  const text = getItemText(item);
  if (!text) return null;

  const handleCopy = async () => {
    await onCopy?.(item, text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  const handleEdit = async () => {
    await onEdit?.(item, text);
  };

  const handleDelete = async () => {
    await onDelete?.(item);
  };

  const hasActions = Boolean(onCopy || onEdit || onDelete);

  return (
    <div className="w-full flex flex-col group items-end" role="listitem">
      <div className="flex flex-col max-w-full items-end">
        <div
          className="max-w-full px-3 py-2 rounded-2xl rounded-br-sm leading-relaxed prose prose-sm prose-invert max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
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
        {hasActions && (
          <div className="flex items-center gap-1 mt-1 self-end justify-end">
            {onCopy && (
              <TextTooltip content="复制">
                <Button
                  variant="ghost"
                  size="sm"
                  className="opacity-0 group-hover:opacity-100 transition-opacity p-0 h-7 w-7"
                  onClick={handleCopy}
                  aria-label="复制消息"
                >
                  {copied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
                </Button>
              </TextTooltip>
            )}
            {onEdit && (
              <TextTooltip content="编辑">
                <Button
                  variant="ghost"
                  size="sm"
                  className="opacity-0 group-hover:opacity-100 transition-opacity p-0 h-7 w-7"
                  onClick={handleEdit}
                  aria-label="编辑消息"
                >
                  <Pencil className="h-4 w-4" />
                </Button>
              </TextTooltip>
            )}
            {onDelete && (
              <TextTooltip content="删除">
                <Button
                  variant="ghost"
                  size="sm"
                  className="opacity-0 group-hover:opacity-100 transition-opacity p-0 h-7 w-7"
                  onClick={handleDelete}
                  aria-label="删除消息"
                >
                  <Trash2 className="h-4 w-4" style={{ color: 'var(--destructive)' }} />
                </Button>
              </TextTooltip>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
