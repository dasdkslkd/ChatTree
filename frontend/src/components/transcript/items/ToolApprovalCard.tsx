import { Check, Loader2, ShieldQuestion, X } from 'lucide-react';
import type { ToolApprovalItem, TranscriptToolApprovalActionHandler } from '../../../types/transcript';

export function ToolApprovalCard({
  item,
  onApproveTool,
  onRejectTool,
  toolApprovalPending = null,
  toolApprovalError = null,
}: {
  item: ToolApprovalItem;
  onApproveTool?: TranscriptToolApprovalActionHandler;
  onRejectTool?: TranscriptToolApprovalActionHandler;
  toolApprovalPending?: string | null;
  toolApprovalError?: string | null;
}) {
  const status = item.status === 'approved'
    ? '已批准'
    : item.status === 'rejected'
      ? '已拒绝'
      : '等待批准';
  const awaiting = item.status === 'awaiting_approval';
  const approving = toolApprovalPending === `${item.id}:approve`;
  const rejecting = toolApprovalPending === `${item.id}:reject`;
  const disabled = toolApprovalPending !== null;

  return (
    <div className="transcript-tool-approval w-full my-2 flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] w-full min-w-0 flex-col gap-2 rounded-md px-3 py-2 text-xs"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-button-tertiary-hover)',
          color: 'var(--fg-secondary)',
        }}
      >
        <div className="flex min-w-0 items-center gap-2">
          <ShieldQuestion className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <span className="shrink-0">工具审批 · {status}</span>
          <span className="min-w-0 truncate">{item.tool_name || item.tool_call_id || ''}</span>
        </div>
        {item.args_preview && <pre className="tc-cmd custom-scrollbar">{item.args_preview}</pre>}
        {item.result_preview && <pre className="tc-output custom-scrollbar">{item.result_preview}</pre>}
        {awaiting && (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              className="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors"
              style={{
                borderColor: 'var(--border)',
                background: 'var(--bg-button-secondary)',
                color: 'var(--fg-primary)',
              }}
              onClick={() => onApproveTool?.(item)}
              disabled={disabled || !onApproveTool}
            >
              {approving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
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
              onClick={() => onRejectTool?.(item)}
              disabled={disabled || !onRejectTool}
            >
              {rejecting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <X className="h-3.5 w-3.5" />}
              拒绝
            </button>
          </div>
        )}
        {toolApprovalError && <div className="text-xs" style={{ color: 'var(--destructive)' }}>{toolApprovalError}</div>}
      </div>
    </div>
  );
}
