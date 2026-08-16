import { memo } from 'react';
import { AlertCircle, Loader2, RotateCw, Square } from 'lucide-react';
import type { RunStatusItem, TranscriptActionHandlers, TranscriptItem } from '../../types/transcript';
import { AssistantAnswerItem } from './items/AssistantAnswerItem';
import { AssistantProcessItem } from './items/AssistantProcessItem';
import { PlanApprovalCard } from './items/PlanApprovalCard';
import { PlanQuestionCard } from './items/PlanQuestionCard';
import { TaskNotificationCard } from './items/TaskNotificationCard';
import { ToolApprovalCard } from './items/ToolApprovalCard';
import { UserMessageItem } from './items/UserMessageItem';

interface TranscriptItemRendererProps extends TranscriptActionHandlers {
  item: TranscriptItem;
}

function UnknownTranscriptItem({ item }: { item: TranscriptItem }) {
  return (
    <div className="transcript-unknown-item w-full flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] min-w-0 items-start gap-2 rounded-md px-2.5 py-1.5 text-xs"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-button-tertiary-hover)',
          color: 'var(--fg-tertiary)',
        }}
      >
        <AlertCircle className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
        <span className="min-w-0 truncate">无法显示的 transcript 项：{item.type || 'unknown'}</span>
      </div>
    </div>
  );
}

function RunStatusTranscriptItem({ item }: { item: RunStatusItem }) {
  const label = (() => {
    if (item.status === 'stopping') return '正在停止';
    if (item.status === 'stopped') return '已停止';
    if (item.status === 'error') return '出错';
    if (item.status === 'reconnecting') return '重新连接中';
    return '运行中';
  })();
  const Icon = item.status === 'stopping'
    ? Loader2
    : item.status === 'error'
      ? AlertCircle
      : item.status === 'running' || item.status === 'reconnecting'
        ? RotateCw
        : Square;
  return (
    <div className="transcript-run-status w-full flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] min-w-0 items-center gap-2 rounded-md px-2.5 py-1.5 text-xs"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-button-secondary)',
          color: item.status === 'error' ? 'var(--destructive)' : 'var(--fg-tertiary)',
        }}
      >
        <Icon className={`h-3.5 w-3.5 shrink-0 ${item.status === 'running' || item.status === 'stopping' || item.status === 'reconnecting' ? 'animate-spin' : ''}`} />
        <span className="min-w-0 whitespace-pre-wrap break-words">{item.message || label}</span>
      </div>
    </div>
  );
}

export const TranscriptItemRenderer = memo(
  function TranscriptItemRenderer({
    item,
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
    planErrorByItem,
    toolApprovalPending,
    toolApprovalErrorByItem,
  }: TranscriptItemRendererProps) {
  switch (item.type) {
    case 'user_message':
      return (
        <UserMessageItem
          item={item}
          onCopy={onCopyItem}
          onEdit={onEditUserMessage}
          onDelete={onDeleteUserMessage}
        />
      );
    case 'assistant_answer':
      return (
        <AssistantAnswerItem
          item={item}
          onCopy={onCopyItem}
          onRetry={onRetryAnswer}
          onEditBranch={onEditBranchAnswer}
        />
      );
    case 'assistant_process':
      return (
        <AssistantProcessItem item={item} />
      );
    case 'plan_question':
      return (
        <PlanQuestionCard
          item={item}
          onAnswerPlanQuestion={onAnswerPlanQuestion}
          planActionPending={planActionPending}
          planErrorByItem={planErrorByItem}
        />
      );
    case 'plan_approval':
      return (
        <PlanApprovalCard
          item={item}
          onApprovePlan={onApprovePlan}
          onRejectPlan={onRejectPlan}
          planActionPending={planActionPending}
          planErrorByItem={planErrorByItem}
        />
      );
    case 'tool_approval':
      return (
        <ToolApprovalCard
          item={item}
          onApproveTool={onApproveTool}
          onRejectTool={onRejectTool}
          toolApprovalPending={toolApprovalPending}
          toolApprovalErrorByItem={toolApprovalErrorByItem}
        />
      );
    case 'task_notification':
      return <TaskNotificationCard item={item} />;
    case 'compact':
      return null;
    case 'run_status':
      return <RunStatusTranscriptItem item={item} />;
    default:
      return <UnknownTranscriptItem item={item} />;
  }
  },
  (prevProps, nextProps) => {
    // 行组件按 item 内容跳过重渲染：未变化的历史消息不再重建 Markdown/折叠等 DOM
    return prevProps.item === nextProps.item
      && prevProps.planActionPending === nextProps.planActionPending
      && prevProps.planErrorByItem === nextProps.planErrorByItem
      && prevProps.toolApprovalPending === nextProps.toolApprovalPending
      && prevProps.toolApprovalErrorByItem === nextProps.toolApprovalErrorByItem;
  },
);
