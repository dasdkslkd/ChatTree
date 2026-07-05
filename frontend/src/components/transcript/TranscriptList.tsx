import { Fragment, type ReactNode } from 'react';
import type { TranscriptActionHandlers, TranscriptItem } from '../../types/transcript';
import { normalizeTranscriptItems } from '../../utils/transcriptItems';
import { TranscriptItemRenderer } from './TranscriptItemRenderer';

interface TranscriptListProps extends TranscriptActionHandlers {
  items: TranscriptItem[];
  isLoading?: boolean;
  transcriptError?: string | null;
  renderItem?: (item: TranscriptItem, defaultItem: ReactNode) => ReactNode;
}

function getItemNodeKey(item: TranscriptItem): string | null {
  return item.node_id || item.anchor_node_id || null;
}

function areSameTurnProcessAndAnswer(previous: TranscriptItem | undefined, current: TranscriptItem | undefined): boolean {
  if (!previous || !current) return false;
  if (previous.type !== 'assistant_process' || current.type !== 'assistant_answer') return false;
  const previousNode = getItemNodeKey(previous);
  const currentNode = getItemNodeKey(current);
  return Boolean(previousNode && currentNode && previousNode === currentNode);
}

function applyProcessAnswerCompaction(items: TranscriptItem[]): TranscriptItem[] {
  return items.map((item, index) => {
    if (areSameTurnProcessAndAnswer(item, items[index + 1])) {
      return {
        ...item,
        props: {
          ...item.props,
          compact_with_next_answer: true,
        },
      };
    }
    if (areSameTurnProcessAndAnswer(items[index - 1], item)) {
      return {
        ...item,
        props: {
          ...item.props,
          compact_after_process: true,
        },
      };
    }
    return item;
  });
}

export function TranscriptList({
  items,
  isLoading = false,
  transcriptError = null,
  onApprovePlan,
  onRejectPlan,
  onAnswerPlanQuestion,
  onCopyItem,
  onEditUserMessage,
  onDeleteUserMessage,
  planActionPending,
  planError,
  renderItem,
}: TranscriptListProps) {
  const normalizedItems = applyProcessAnswerCompaction(normalizeTranscriptItems(items));

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
      {normalizedItems.map((item) => {
        const defaultItem = (
          <TranscriptItemRenderer
            item={item}
            onApprovePlan={onApprovePlan}
            onRejectPlan={onRejectPlan}
            onAnswerPlanQuestion={onAnswerPlanQuestion}
            onCopyItem={onCopyItem}
            onEditUserMessage={onEditUserMessage}
            onDeleteUserMessage={onDeleteUserMessage}
            planActionPending={planActionPending}
            planError={planError}
          />
        );
        return (
          <Fragment key={item.id}>
            {renderItem ? renderItem(item, defaultItem) : defaultItem}
          </Fragment>
        );
      })}
    </div>
  );
}
