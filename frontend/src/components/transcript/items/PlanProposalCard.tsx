import { Check, X } from 'lucide-react';
import MarkdownContent from '../../MarkdownContent';

export interface PlanProposalBlock {
  type: 'plan_proposal';
  tool_name?: 'exit_plan_mode' | string;
  tool_call_id?: string;
  plan_id?: string;
  proposal_id?: string;
  revision?: number;
  status: 'awaiting_approval' | 'approved' | 'rejected' | 'superseded';
  plan: string;
  feedback?: string | null;
}

interface PlanProposalCardProps {
  block: PlanProposalBlock;
  onApprove?: (block: PlanProposalBlock) => void | Promise<void>;
  onReject?: (block: PlanProposalBlock) => void | Promise<void>;
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
  const awaiting = block.status === 'awaiting_approval';
  const body = awaiting ? block.plan : truncatePlan(block.plan);

  return (
    <div
      className={`transcript-plan-card plan-card plan-card-${block.status} w-full my-2 flex flex-col items-start`}
      data-proposal-id={block.proposal_id || block.tool_call_id}
      data-plan-id={block.plan_id}
    >
      <div
        className="flex max-w-[760px] w-full min-w-0 flex-col gap-1.5 text-sm"
        style={{ color: 'var(--fg-secondary)' }}
      >
        <div className="plan-card-header flex items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
          <span className="plan-card-title">计划</span>
          <span className="plan-card-status">· {statusLabel(block.status)}</span>
        </div>
        <div
          className={`plan-card-body min-w-0 prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2${awaiting ? '' : ' compact'}`}
          style={{
            color: 'var(--fg-secondary)',
            fontSize: 'var(--codex-chat-font-size)',
            lineHeight: 'calc(var(--codex-chat-font-size) + 8px)',
          }}
        >
          <MarkdownContent enableMermaid>{body}</MarkdownContent>
        </div>
        {block.feedback && (
          <div className="plan-card-feedback text-xs" style={{ color: 'var(--fg-tertiary)' }}>
            {block.feedback}
          </div>
        )}
        {awaiting && (
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
        )}
        {error && awaiting && (
          <div className="text-xs" style={{ color: 'var(--destructive)' }}>{error}</div>
        )}
      </div>
    </div>
  );
}

function truncatePlan(plan: string): string {
  return plan.length > 420 ? `${plan.slice(0, 420).trimEnd()}...` : plan;
}

function statusLabel(status: PlanProposalBlock['status']): string {
  if (status === 'awaiting_approval') return '等待批准';
  if (status === 'approved') return '已批准';
  if (status === 'rejected') return '已驳回';
  return '已被新计划取代';
}
