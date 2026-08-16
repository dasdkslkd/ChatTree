import { useState } from 'react';
import { Check, ChevronRight, ClipboardList, Loader2, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import MarkdownContent from '../../MarkdownContent';
import type { PlanApprovalItem, TranscriptPlanActionHandler } from '../../../types/transcript';

function statusLabel(status: string | null | undefined, pending: string | null): string {
  if (pending === 'approve') return '实现中';
  if (pending === 'reject') return '处理中';
  if (status === 'approved') return '已批准';
  if (status === 'rejected') return '已要求修改';
  return '等待批准';
}

export function PlanApprovalCard({
  item,
  onApprovePlan,
  onRejectPlan,
  planActionPending = null,
  planErrorByItem = {},
}: {
  item: PlanApprovalItem;
  onApprovePlan?: TranscriptPlanActionHandler;
  onRejectPlan?: TranscriptPlanActionHandler;
  planActionPending?: string | null;
  planErrorByItem?: Record<string, string>;
}) {
  const awaiting = item.status === 'awaiting_approval';
  const [expanded, setExpanded] = useState(awaiting);
  const label = statusLabel(item.status, planActionPending);
  const title = (item.plan || '').split(/\r?\n/).map((line) => line.trim().replace(/^#+\s*/, '')).find(Boolean) || '计划';

  return (
    <div className="transcript-plan-approval w-full flex flex-col items-start" role="listitem">
      <div className="flex max-w-[760px] w-full min-w-0 flex-col gap-2 text-sm" style={{ color: 'var(--fg-secondary)' }}>
        <div className="flex min-w-0 items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
          <ClipboardList className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <button
            type="button"
            className="flex min-w-0 flex-1 items-center gap-2 text-left"
            style={{ color: 'inherit' }}
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
          >
            <span className="shrink-0">计划 · {label}</span>
            {!expanded && <span className="min-w-0 truncate font-normal">{title}</span>}
            <ChevronRight className={cn('h-3.5 w-3.5 shrink-0 transition-transform', expanded && 'rotate-90')} />
          </button>
        </div>
        {expanded && item.plan && (
          <div
            className="plan-markdown-panel min-w-0 prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
            style={{
              color: 'var(--fg-secondary)',
              fontSize: 'var(--codex-chat-font-size)',
              lineHeight: 'calc(var(--codex-chat-font-size) + 8px)',
            }}
          >
            <MarkdownContent enableMermaid>{item.plan}</MarkdownContent>
          </div>
        )}
        {item.feedback && (
          <div className="text-xs break-words" style={{ color: 'var(--fg-tertiary)' }}>{item.feedback}</div>
        )}
        {awaiting && expanded && (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              className="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors"
              style={{
                borderColor: 'var(--border)',
                background: 'var(--bg-button-secondary)',
                color: 'var(--fg-85)',
              }}
              onClick={() => onApprovePlan?.(item)}
              disabled={planActionPending !== null}
            >
              {planActionPending === 'approve' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              批准
            </button>
            <button
              type="button"
              className="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors"
              style={{
                borderColor: 'var(--border)',
                background: 'var(--bg-button-secondary)',
                color: 'var(--fg-secondary)',
              }}
              onClick={() => onRejectPlan?.(item)}
              disabled={planActionPending !== null}
            >
              <X className="h-3.5 w-3.5" />
              要求修改
            </button>
          </div>
        )}
        {planErrorByItem[item.id] && <div className="text-xs" style={{ color: 'var(--destructive)' }}>{planErrorByItem[item.id]}</div>}
      </div>
    </div>
  );
}
