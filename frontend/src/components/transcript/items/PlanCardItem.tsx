import { Check, ClipboardList, X } from 'lucide-react';
import MarkdownContent from '../../MarkdownContent';
import type { TranscriptItem, TranscriptPlanActionHandler } from '../../../types/transcript';
import { getItemText, getStatusText } from './itemText';

interface PlanCardItemProps {
  item: TranscriptItem;
  onApprovePlan?: TranscriptPlanActionHandler;
  onRejectPlan?: TranscriptPlanActionHandler;
}

export function PlanCardItem({ item, onApprovePlan, onRejectPlan }: PlanCardItemProps) {
  const text = getItemText(item, 'Plan update');
  const status = getStatusText(item);
  const isAwaitingApproval = status === 'awaiting_approval';

  return (
    <div className="transcript-plan-card w-full my-2 flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] w-full min-w-0 flex-col gap-1.5 text-sm"
        style={{
          color: 'var(--fg-secondary)',
        }}
      >
        <div className="flex items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
          <ClipboardList className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <span>Plan{status ? ` · ${status}` : ''}</span>
        </div>
        <div
          className="min-w-0 prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
          style={{
            color: 'var(--fg-secondary)',
            fontSize: 'var(--codex-chat-font-size)',
            lineHeight: 'calc(var(--codex-chat-font-size) + 8px)',
          }}
        >
          <MarkdownContent enableMermaid>{text}</MarkdownContent>
        </div>
        {isAwaitingApproval && (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              className="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors"
              style={{
                borderColor: 'var(--border)',
                background: 'var(--bg-button-secondary)',
                color: 'var(--fg-primary)',
              }}
              onClick={() => onApprovePlan?.(item)}
            >
              <Check className="h-3.5 w-3.5" />
              批准
            </button>
            <button
              type="button"
              className="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors"
              style={{
                borderColor: 'var(--border)',
                background: 'var(--bg-button-tertiary)',
                color: 'var(--fg-secondary)',
              }}
              onClick={() => onRejectPlan?.(item)}
            >
              <X className="h-3.5 w-3.5" />
              要求修改
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
