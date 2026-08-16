import { Fragment, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ChevronRight, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import MarkdownContent from '../MarkdownContent';
import type { TranscriptActionHandlers, TranscriptItem } from '../../types/transcript';
import { formatProcessedDuration } from '../../utils/time';
import { TranscriptItemRenderer } from './TranscriptItemRenderer';

const PROCESS_ITEM_TYPES = new Set<TranscriptItem['type']>([
  'assistant_process',
  'plan_question',
  'plan_approval',
  'tool_approval',
  'run_status',
  'task_notification',
]);

function PendingUserMessage({ content }: { content: string }) {
  return (
    <div className="w-full flex flex-col group items-end" role="listitem">
      <div className="flex flex-col max-w-full min-w-0 items-end">
        <div
          className="max-w-full min-w-0 break-words px-3 py-2 rounded-2xl rounded-br-sm leading-relaxed prose prose-sm prose-invert [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
          style={{
            background: 'linear-gradient(160deg, color-mix(in srgb, var(--accent-soft) 45%, transparent), color-mix(in srgb, var(--accent-soft) 25%, transparent))',
            border: '0.5px solid color-mix(in srgb, var(--icon-accent) 28%, transparent)',
            boxShadow: 'var(--highlight-top)',
            color: 'var(--fg-85)',
            fontSize: 'var(--codex-chat-font-size)',
            lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
          }}
        >
          <MarkdownContent enableMermaid>{content}</MarkdownContent>
        </div>
        <span
          className="mt-1 flex items-center gap-1 self-end text-[11px]"
          style={{ color: 'var(--fg-tertiary)' }}
          role="status"
        >
          <Loader2 className="h-3 w-3 animate-spin" />
          发送中
        </span>
      </div>
    </div>
  );
}

interface TranscriptListProps extends TranscriptActionHandlers {
  items: TranscriptItem[];
  isLoading?: boolean;
  transcriptError?: string | null;
  pendingUserItems?: Array<{ id: string; content: string }>;
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
  const streaming = items.some((item) => (item as { status?: string }).status === 'running');
  const [expanded, setExpanded] = useState(streaming);
  const [durationMs, setDurationMs] = useState(totalDuration);
  useEffect(() => {
    if (!streaming) setExpanded(false);
  }, [streaming]);
  useEffect(() => {
    if (!streaming) return;
    const baseAt = Date.now() - totalDuration;
    const timer = window.setInterval(() => {
      setDurationMs(Math.max(totalDuration, Date.now() - baseAt));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [streaming, totalDuration]);
  const duration = streaming ? durationMs : totalDuration;
  return (
    <div className="w-full flex flex-col items-start" role="listitem">
      <div className={cn('processed-fold', expanded && 'expanded')}>
        <button
          type="button"
          className="processed-fold-button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <span>{duration > 0 ? `已处理 ${formatProcessedDuration(duration) ?? ''}`.trim() : '已处理'}</span>
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
  pendingUserItems = [],
  onApprovePlan,
  onRejectPlan,
  onAnswerPlanQuestion,
  onApproveTool,
  onRejectTool,
  onCopyItem,
  onEditUserMessage,
  onDeleteUserMessage,
  onRetryAnswer,
  onEditBranchAnswer,
  planActionPending,
  planError,
  toolApprovalPending,
  toolApprovalError,
  renderItem,
}: TranscriptListProps) {
  // 数据契约：MainPage 入口（onSnapshot / applyTranscriptPatch）已逐 item 归一化（幂等），此处直接消费
  const groups = useMemo(() => groupTranscriptItems(items), [items]);
  const renderDefault = useCallback(
    (item: TranscriptItem) => (
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
        onRetryAnswer={onRetryAnswer}
        onEditBranchAnswer={onEditBranchAnswer}
        planActionPending={planActionPending}
        planError={planError}
        toolApprovalPending={toolApprovalPending}
        toolApprovalError={toolApprovalError}
      />
    ),
    [onApprovePlan, onRejectPlan, onAnswerPlanQuestion, onApproveTool, onRejectTool, onCopyItem, onEditUserMessage, onDeleteUserMessage, onRetryAnswer, onEditBranchAnswer, planActionPending, planError, toolApprovalPending, toolApprovalError],
  );

  if (items.length === 0) {
    return (
      <div className="transcript-list flex w-full flex-col" role="list">
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
    <div className="transcript-list flex w-full flex-col" role="list">
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
      {pendingUserItems.map((pending) => (
        <PendingUserMessage key={pending.id} content={pending.content} />
      ))}
    </div>
  );
}
