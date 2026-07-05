import type { TranscriptItem, TranscriptPlanActionHandler } from '../../../types/transcript';
import {
  getActiveReasoningKey,
  normalizeLegacyToolInteractions,
  normalizePersistedAssistantTimeline,
  type AssistantProcessRenderProps,
} from '../../../utils/assistantTimeline';
import { getStreamingTimelineFoldState } from '../../../utils/assistantTimelineFolding';
import { AssistantProcessTimeline } from './AssistantProcessTimeline';
import { getItemText, getStatusText } from './itemText';

interface AssistantProcessItemProps {
  item: TranscriptItem;
  onApprovePlan?: TranscriptPlanActionHandler;
  onRejectPlan?: TranscriptPlanActionHandler;
  planActionPending?: string | null;
  planError?: string | null;
}

function getProcessProps(item: TranscriptItem): AssistantProcessRenderProps {
  if (item.props?.live_process && Array.isArray(item.props.timeline)) {
    return item.props as unknown as AssistantProcessRenderProps;
  }

  const timeline = Array.isArray(item.props?.timeline)
    ? normalizePersistedAssistantTimeline(item.props.timeline)
    : normalizeLegacyToolInteractions({
      content: item.preview,
      reasoning: item.props?.reasoning,
      tool_interactions: Array.isArray(item.props?.tool_interactions) ? item.props.tool_interactions : [],
    });

  const fallbackText = timeline.length === 0 ? getItemText(item, 'Processing') : '';
  const renderTimeline = timeline.length === 0 && fallbackText
    ? [{ type: 'reasoning' as const, key: 'reasoning-fallback', reasoning: fallbackText }]
    : timeline;
  const status = item.status || getStatusText(item) || null;
  const duration = typeof item.props?.duration === 'number' ? item.props.duration : 0;
  const errorMessage = typeof item.props?.errorMessage === 'string' ? item.props.errorMessage : null;
  return {
    live_process: false,
    pendingUserMessage: null,
    showPendingBubble: false,
    showStreamBlock: true,
    timeline: renderTimeline,
    streamingFoldState: getStreamingTimelineFoldState(renderTimeline, []),
    activeReasoningKey: getActiveReasoningKey(renderTimeline, {
      status: status === 'streaming' ? 'streaming' : 'completed',
      reasoningActive: false,
    }),
    status,
    duration,
    errorMessage,
    content: item.preview || '',
  };
}

export function AssistantProcessItem({
  item,
  onApprovePlan,
  onRejectPlan,
  planActionPending = null,
  planError = null,
}: AssistantProcessItemProps) {
  return (
    <AssistantProcessTimeline
      item={item}
      props={getProcessProps(item)}
      onApprovePlan={onApprovePlan}
      onRejectPlan={onRejectPlan}
      planActionPending={planActionPending}
      planError={planError}
    />
  );
}
