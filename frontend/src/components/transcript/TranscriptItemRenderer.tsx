import { AlertCircle } from 'lucide-react';
import type { TranscriptActionHandlers, TranscriptItem } from '../../types/transcript';
import { AssistantAnswerItem } from './items/AssistantAnswerItem';
import { AssistantProcessItem } from './items/AssistantProcessItem';
import { CompactItem } from './items/CompactItems';
import { PlanCardItem } from './items/PlanCardItem';
import { RunDraftItem } from './items/RunDraftItem';
import { SideRunNotificationItem } from './items/SideRunNotificationItem';
import { TaskNotificationItem } from './items/TaskNotificationItem';
import { TaskProgressItem } from './items/TaskProgressItem';
import { ToolGroupItem } from './items/ToolGroupItem';
import { UserMessageItem } from './items/UserMessageItem';

interface TranscriptItemRendererProps extends TranscriptActionHandlers {
  item: TranscriptItem;
}

function UnknownTranscriptItem({ item }: { item: TranscriptItem }) {
  return (
    <div className="transcript-unknown-item w-full my-2 flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] min-w-0 items-center gap-2 rounded-md px-2.5 py-1.5 text-xs"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-button-tertiary-hover)',
          color: 'var(--fg-tertiary)',
        }}
      >
        <AlertCircle className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
        <span className="min-w-0 truncate">无法显示的 transcript 项：{item.item_type || item.type || 'unknown'}</span>
      </div>
    </div>
  );
}

export function TranscriptItemRenderer({
  item,
  onApprovePlan,
  onRejectPlan,
  onCopyItem,
}: TranscriptItemRendererProps) {
  switch (item.type) {
    case 'user_message':
      return <UserMessageItem item={item} onCopy={onCopyItem} />;
    case 'assistant_answer':
      return <AssistantAnswerItem item={item} onCopy={onCopyItem} />;
    case 'assistant_process':
      return <AssistantProcessItem item={item} />;
    case 'tool_group':
      return <ToolGroupItem item={item} />;
    case 'plan_card':
      return <PlanCardItem item={item} onApprovePlan={onApprovePlan} onRejectPlan={onRejectPlan} />;
    case 'task_notification':
      return <TaskNotificationItem item={item} />;
    case 'task_progress':
      return <TaskProgressItem item={item} />;
    case 'run_draft':
      return <RunDraftItem item={item} />;
    case 'side_run_notification':
      return <SideRunNotificationItem item={item} />;
    case 'compact_boundary':
    case 'compact_summary':
      return <CompactItem item={item} />;
    default:
      return <UnknownTranscriptItem item={item} />;
  }
}
