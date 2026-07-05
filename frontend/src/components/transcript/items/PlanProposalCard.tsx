import { Check, X } from 'lucide-react';
import MarkdownContent from '../../MarkdownContent';

export interface PlanApprovalBlock {
  plan_id?: string;
  status: 'awaiting_approval';
  plan: string;
}

interface PlanProposalCardProps {
  block: PlanApprovalBlock;
  onApprove?: (block: PlanApprovalBlock) => void | Promise<void>;
  onReject?: (block: PlanApprovalBlock) => void | Promise<void>;
  pending?: boolean;
  error?: string | null;
}

export function PlanProposalCard({
  block,
  onApprove,
  onReject,
  pending = false,
  error = null,
}: PlanProposalCardProps) {
  return (
    <div
      className="transcript-plan-card plan-card plan-card-awaiting_approval w-full my-2 flex flex-col items-start"
      data-plan-id={block.plan_id}
    >
      <div
        className="flex max-w-[760px] w-full min-w-0 flex-col gap-1.5 text-sm"
        style={{ color: 'var(--fg-secondary)' }}
      >
        <div className="plan-card-header flex items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
          <span className="plan-card-title">计划</span>
          <span className="plan-card-status">· 等待批准</span>
        </div>
        <div
          className="plan-card-body min-w-0 prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
          style={{
            color: 'var(--fg-secondary)',
            fontSize: 'var(--codex-chat-font-size)',
            lineHeight: 'calc(var(--codex-chat-font-size) + 8px)',
          }}
        >
          <MarkdownContent enableMermaid>{block.plan}</MarkdownContent>
        </div>
        <div className="plan-card-actions flex flex-wrap items-center gap-2 pt-1">
          <button
            type="button"
            className="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors"
            style={{
              borderColor: 'var(--border)',
              background: 'var(--bg-button-secondary)',
              color: 'var(--fg-primary)',
            }}
            onClick={() => { void onApprove?.(block); }}
            disabled={pending}
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
            onClick={() => { void onReject?.(block); }}
            disabled={pending}
          >
            <X className="h-3.5 w-3.5" />
            驳回
          </button>
        </div>
        {error && (
          <div className="text-xs" style={{ color: 'var(--destructive)' }}>{error}</div>
        )}
      </div>
    </div>
  );
}
