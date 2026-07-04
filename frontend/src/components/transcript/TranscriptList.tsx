import type { TranscriptActionHandlers, TranscriptItem } from '../../types/transcript';
import { normalizeTranscriptItems } from '../../utils/transcriptItems';
import { TranscriptItemRenderer } from './TranscriptItemRenderer';

interface TranscriptListProps extends TranscriptActionHandlers {
  items: TranscriptItem[];
  isLoading?: boolean;
  transcriptError?: string | null;
}

export function TranscriptList({
  items,
  isLoading = false,
  transcriptError = null,
  onApprovePlan,
  onRejectPlan,
  onCopyItem,
}: TranscriptListProps) {
  const normalizedItems = normalizeTranscriptItems(items);

  if (normalizedItems.length === 0) {
    return (
      <div className="transcript-list flex w-full flex-col gap-2" role="list">
        {transcriptError && (
          <div className="transcript-error py-4 text-center text-sm" style={{ color: 'var(--destructive)' }} role="status">
            {transcriptError}
          </div>
        )}
        <div className="transcript-empty py-8 text-center text-sm" style={{ color: 'var(--fg-tertiary)' }} role="listitem">
          {isLoading ? '正在加载对话...' : '暂无对话内容'}
        </div>
      </div>
    );
  }

  return (
    <div className="transcript-list flex w-full flex-col gap-1" role="list">
      {transcriptError && (
        <div className="transcript-error py-2 text-center text-xs" style={{ color: 'var(--destructive)' }} role="status">
          {transcriptError}
        </div>
      )}
      {normalizedItems.map((item) => (
        <TranscriptItemRenderer
          key={item.id}
          item={item}
          onApprovePlan={onApprovePlan}
          onRejectPlan={onRejectPlan}
          onCopyItem={onCopyItem}
        />
      ))}
    </div>
  );
}
