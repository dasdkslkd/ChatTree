import type { TranscriptItem } from '../../types/transcript';
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

export function TranscriptItemRenderer({ item }: { item: TranscriptItem }) {
  switch (item.type) {
    case 'user_message':
      return <UserMessageItem item={item} />;
    case 'assistant_answer':
      return <AssistantAnswerItem item={item} />;
    case 'assistant_process':
      return <AssistantProcessItem item={item} />;
    case 'tool_group':
      return <ToolGroupItem item={item} />;
    case 'plan_card':
      return <PlanCardItem item={item} />;
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
      return null;
  }
}
