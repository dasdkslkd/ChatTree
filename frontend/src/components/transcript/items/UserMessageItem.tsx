import { useEffect, useState } from 'react';
import { Check, Copy, FileText, Loader2, Pencil, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { TextTooltip } from '@/components/ui/text-tooltip';
import MarkdownContent from '../../MarkdownContent';
import { conversationApi } from '../../../api/conversation';
import type {
  TranscriptCopyHandler,
  UserMessageItem as UserMessageTranscriptItem,
  TranscriptUserMessageActionHandler,
  TranscriptUserMessageDeleteHandler,
} from '../../../types/transcript';
import { getItemText } from './itemText';

type PreviewTarget = {
  filename: string;
  isImage: boolean;
};

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
  const [imageUrls, setImageUrls] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<PreviewTarget | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewText, setPreviewText] = useState<string | null>(null);

  const text = getItemText(item);
  const importFiles = (item.import_files ?? []).map((file) => file.filename).filter(Boolean);
  const imageRefs = (item.image_refs ?? []).filter((file) => Boolean(file.filename));
  const hasAttachments = importFiles.length > 0 || imageRefs.length > 0;
  const imageKey = imageRefs.map((file) => file.filename).join('|');

  useEffect(() => {
    if (!imageRefs.length) return;
    const objectUrls: string[] = [];
    let cancelled = false;
    for (const ref of imageRefs) {
      conversationApi
        .fetchImportBlob(item.conversation_id, ref.filename)
        .then((blob) => {
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          objectUrls.push(url);
          setImageUrls((prev) => ({ ...prev, [ref.filename]: url }));
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
      for (const url of objectUrls) URL.revokeObjectURL(url);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.conversation_id, imageKey]);

  const openPreview = async (target: PreviewTarget) => {
    setPreview(target);
    setPreviewUrl(target.isImage ? imageUrls[target.filename] ?? null : null);
    setPreviewText(null);
    if (target.isImage && imageUrls[target.filename]) return;
    try {
      const blob = await conversationApi.fetchImportBlob(item.conversation_id, target.filename);
      if (target.isImage) {
        setPreviewUrl(URL.createObjectURL(blob));
      } else {
        setPreviewText(await blob.text());
      }
    } catch {
      if (!target.isImage) setPreviewText('附件内容加载失败');
    }
  };

  if (!text && !hasAttachments) return null;

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
      <div className="flex flex-col max-w-full min-w-0 items-end">
        {hasAttachments && (
          <div className="flex flex-wrap items-center justify-end gap-1.5 mb-1">
            {importFiles.map((filename) => (
              <TextTooltip key={filename} content={filename}>
                <button
                  type="button"
                  onClick={() => openPreview({ filename, isImage: false })}
                  className="flex max-w-[240px] items-center gap-1 rounded-md px-2 py-1 text-xs cursor-pointer hover:opacity-80 transition-opacity"
                  style={{
                    border: '0.5px solid rgba(217,119,87,0.28)',
                    background: 'rgba(217,119,87,0.08)',
                    color: 'var(--fg-85)',
                  }}
                >
                  <FileText className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">{filename}</span>
                </button>
              </TextTooltip>
            ))}
            {imageRefs.map((ref) => {
              const url = imageUrls[ref.filename];
              return url ? (
                <img
                  key={ref.filename}
                  src={url}
                  alt={ref.filename}
                  onClick={() => openPreview({ filename: ref.filename, isImage: true })}
                  className="h-20 max-w-[160px] object-cover rounded-md cursor-pointer hover:opacity-80 transition-opacity"
                  style={{ border: '0.5px solid rgba(217,119,87,0.28)' }}
                />
              ) : (
                <div
                  key={ref.filename}
                  className="h-20 w-20 flex items-center justify-center rounded-md"
                  style={{ border: '0.5px solid rgba(217,119,87,0.28)' }}
                >
                  <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--fg-tertiary)' }} />
                </div>
              );
            })}
          </div>
        )}
        {text ? (
          <div
            className="max-w-full min-w-0 break-words px-3 py-2 rounded-2xl rounded-br-sm leading-relaxed prose prose-sm prose-invert [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
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
        ) : null}
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
      <Dialog open={preview !== null} onOpenChange={(open) => { if (!open) setPreview(null); }}>
        <DialogContent className="sm:max-w-3xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="truncate">{preview?.filename}</DialogTitle>
          </DialogHeader>
          <div className="overflow-auto min-h-0 min-w-0 break-words [overflow-wrap:anywhere] [&_pre]:overflow-x-auto [&_pre]:[overflow-wrap:normal]">
            {preview?.isImage ? (
              previewUrl ? (
                <img src={previewUrl} alt={preview.filename} className="max-w-full" />
              ) : (
                <div className="flex justify-center py-8">
                  <Loader2 className="h-5 w-5 animate-spin" style={{ color: 'var(--fg-tertiary)' }} />
                </div>
              )
            ) : previewText !== null ? (
              <MarkdownContent>{previewText}</MarkdownContent>
            ) : (
              <div className="flex justify-center py-8">
                <Loader2 className="h-5 w-5 animate-spin" style={{ color: 'var(--fg-tertiary)' }} />
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
