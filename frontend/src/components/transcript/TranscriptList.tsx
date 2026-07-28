import { Fragment, useState, type ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { TranscriptActionHandlers, TranscriptItem } from '../../types/transcript';
import { formatProcessedDuration } from '../../utils/time';
import { normalizeTranscriptItems } from '../../utils/transcriptItems';
import { TranscriptItemRenderer } from './TranscriptItemRenderer';

const PROCESS_ITEM_TYPES = new Set<TranscriptItem['type']>([
  'assistant_process',
  'plan_question',
  'plan_approval',
  'tool_approval',
  'run_status',
  'task_notification',
]);

interface TranscriptListProps extends TranscriptActionHandlers {
  items: TranscriptItem[];
  isLoading?: boolean;
  transcriptError?: string | null;
  renderItem?: (item: TranscriptItem, defaultItem: ReactNode) => ReactNode;
}

function ProcessedItemsFold({
  items,
  totalDuration,
  renderInner,
}: {
  items: TranscriptItem[];
  totalDuration: number;
  renderInner: (item: TranscriptItem) => ReactNode;
}) {
  const [expanded, setExpanded] = useState(true);
  return (
    <div className="w-full flex flex-col items-start" role="listitem">
      <div className={cn('processed-fold', expanded && 'expanded')}>
        <button
          type="button"
          className="processed-fold-button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <span>{totalDuration > 0 ? `已处理 ${formatProcessedDuration(totalDuration) ?? ''}`.trim() : '已处理'}</span>
          <ChevronRight className="processed-fold-chevron" />
        </button>
      </div>
      <div className={cn('processed-blocks-shell', expanded && 'expanded')}>
        <div className="processed-blocks-inner">
          {items.map((item) => <Fragment key={item.id}>{renderInner(item)}</Fragment>)}
        </div>
      </div>
    </div>
  );
}

type TranscriptGroup =
  | { kind: 'single'; item: TranscriptItem }
  | { kind: 'process'; items: TranscriptItem[]; duration: number };

function groupTranscriptItems(items: TranscriptItem[]): TranscriptGroup[] {
  const groups: TranscriptGroup[] = [];
  let pending: TranscriptItem[] = [];
  const flush = () => {
    if (pending.length === 0) return;
    const duration = pending.reduce((sum, item) => {
      const value = (item as { duration_ms?: number | null }).duration_ms;
      return sum + (typeof value === 'number' ? value : 0);
    }, 0);
    groups.push({ kind: 'process', items: pending, duration });
    pending = [];
  };
  for (const item of items) {
    if (PROCESS_ITEM_TYPES.has(item.type)) {
      pending.push(item);
    } else {
      flush();
      groups.push({ kind: 'single', item });
    }
  }
  flush();
  return groups;
}

export function TranscriptList({
  items,
  isLoading = false,
  transcriptError = null,
  onApprovePlan,
  onRejectPlan,
  onAnswerPlanQuestion,
  onApproveTool,
  onRejectTool,
  onCopyItem,
  onEditUserMessage,
  onDeleteUserMessage,
  planActionPending,
  planError,
  toolApprovalPending,
  toolApprovalError,
  renderItem,
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

  const renderDefault = (item: TranscriptItem) => (
    <TranscriptItemRenderer
      item={item}
      onApprovePlan={onApprovePlan}
      onRejectPlan={onRejectPlan}
      onAnswerPlanQuestion={onAnswerPlanQuestion}
      onApproveTool={onApproveTool}
      onRejectTool={onRejectTool}
      onCopyItem={onCopyItem}
      onEditUserMessage={onEditUserMessage}
      onDeleteUserMessage={onDeleteUserMessage}
      planActionPending={planActionPending}
      planError={planError}
      toolApprovalPending={toolApprovalPending}
      toolApprovalError={toolApprovalError}
    />
  );

  const groups = groupTranscriptItems(normalizedItems);

  return (
    <div className="transcript-list flex w-full flex-col gap-2" role="list">
      {transcriptError && (
        <div className="transcript-error py-2 text-center text-xs" style={{ color: 'var(--destructive)' }} role="status">
          {transcriptError}
        </div>
      )}
      {groups.map((group, index) => {
        if (group.kind === 'single') {
          const item = group.item;
          const defaultItem = renderDefault(item);
          return (
            <Fragment key={item.id}>
              {renderItem ? renderItem(item, defaultItem) : defaultItem}
            </Fragment>
          );
        }
        return (
          <ProcessedItemsFold
            key={`fold-${index}`}
            items={group.items}
            totalDuration={group.duration}
            renderInner={renderDefault}
          />
        );
      })}
    </div>
  );
}
