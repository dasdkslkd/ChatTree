import { Check, Loader2, ShieldQuestion, X } from 'lucide-react';
import type { ToolApprovalItem, TranscriptToolApprovalActionHandler } from '../../../types/transcript';
import { ToolCallPreview } from './ToolCallRenderer';

export function ToolApprovalCard({
  item,
  onApproveTool,
  onRejectTool,
  toolApprovalPending = null,
  toolApprovalErrorByItem = {},
}: {
  item: ToolApprovalItem;
  onApproveTool?: TranscriptToolApprovalActionHandler;
  onRejectTool?: TranscriptToolApprovalActionHandler;
  toolApprovalPending?: string | null;
  toolApprovalErrorByItem?: Record<string, string>;
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
  const hasPreview = Boolean(item.args_preview || item.result_preview);

  return (
    <div className="transcript-tool-approval w-full flex flex-col items-start" role="listitem">
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
        {hasPreview && (
          <ToolCallPreview
            toolName={item.tool_name}
            argsText={item.args_preview || ''}
            outputText={item.result_preview}
          />
        )}
        {awaiting && (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              className="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors"
              style={{
                borderColor: 'var(--border)',
                background: 'var(--bg-button-secondary)',
                color: 'var(--fg-85)',
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
                background: 'var(--bg-button-secondary)',
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
        {toolApprovalErrorByItem[item.id] && <div className="text-xs" style={{ color: 'var(--destructive)' }}>{toolApprovalErrorByItem[item.id]}</div>}
      </div>
    </div>
  );
}
