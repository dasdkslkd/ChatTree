import type { TranscriptItem } from '../types/transcript';

const transcriptItemTypes = new Set([
  'compact_boundary',
  'compact_summary',
  'user_message',
  'assistant_process',
  'assistant_answer',
  'tool_group',
  'task_notification',
  'task_progress',
  'run_draft',
  'side_run_notification',
]);

function normalizeTranscriptItem(item: TranscriptItem): TranscriptItem | null {
  const rawType = item.type || item.item_type;
  if (rawType === 'plan_card') {
    const status = String(item.status || item.props?.status || '');
    const plan = typeof item.props?.plan === 'string' ? item.props.plan : item.preview || '';
    return status === 'awaiting_approval' && plan.trim()
      ? { ...item, type: 'plan_card' }
      : null;
  }
  if (item.type) return item;
  const itemType = typeof item.item_type === 'string' && transcriptItemTypes.has(item.item_type)
    ? item.item_type
    : undefined;
  return itemType ? { ...item, type: itemType as TranscriptItem['type'] } : item;
}

function processKeys(item: TranscriptItem): string[] {
  const keys: string[] = [];
  const nodeId = item.node_id || item.anchor_node_id || null;
  const runId = item.run_id || null;
  if (runId) keys.push(`run:${runId}`);
  if (nodeId) keys.push(`node:${nodeId}`);
  if (item.anchor_node_id && item.anchor_node_id !== nodeId) keys.push(`node:${item.anchor_node_id}`);
  return keys;
}

function filterStaleRunDraftItems(items: TranscriptItem[]): TranscriptItem[] {
  const processKeySet = new Set(
    items
      .filter((item) => item.type === 'assistant_process')
      .flatMap(processKeys),
  );
  if (processKeySet.size === 0) return items;
  return items.filter((item) => {
    if (item.type === 'run_draft') {
      const keys = processKeys(item);
      return keys.length === 0 || !keys.some((key) => processKeySet.has(key));
    }
    return true;
  });
}

function getContinuationTargetNodeId(item: TranscriptItem): string | null {
  const props = item.props || {};
  const target = props.continuation_of_node_id || props.continuationOfNodeId;
  if (typeof target === 'string' && target.length > 0) return target;
  const origin = props.origin;
  if (
    item.type === 'assistant_process'
    && (origin === 'plan_approval' || origin === 'plan_question_answer' || origin === 'plan_reject' || origin === 'plan_rejection')
    && item.anchor_node_id
  ) {
    return item.anchor_node_id;
  }
  return null;
}

function getContinuationMarker(item: TranscriptItem): string {
  const marker = item.props?.continuation_marker || item.props?.marker;
  if (typeof marker === 'string' && marker.trim()) return marker;
  const origin = item.props?.origin;
  return origin === 'plan_approval' ? '计划已批准，开始实现' : '计划反馈已提交，继续计划';
}

function getTimelineBlocks(item: TranscriptItem): unknown[] {
  return Array.isArray(item.props?.timeline) ? item.props.timeline : [];
}

function mergeContinuationProcessItems(items: TranscriptItem[]): TranscriptItem[] {
  const merged: TranscriptItem[] = [];
  for (const item of items) {
    if (item.type !== 'assistant_process') {
      merged.push(item);
      continue;
    }

    const targetNodeId = getContinuationTargetNodeId(item);
    if (!targetNodeId) {
      const continuations = Array.isArray(item.props?.continuations) ? item.props.continuations : [];
      if (continuations.length > 0) {
        const timeline = [...getTimelineBlocks(item)];
        for (const continuation of continuations) {
          if (!continuation || typeof continuation !== 'object') continue;
          const record = continuation as Record<string, unknown>;
          const marker = typeof record.marker === 'string' && record.marker.trim()
            ? record.marker
            : typeof record.continuation_marker === 'string'
              ? record.continuation_marker
              : '';
          if (marker) timeline.push({ type: 'marker', content: marker });
          if (Array.isArray(record.timeline)) timeline.push(...record.timeline);
        }
        merged.push({
          ...item,
          props: {
            ...item.props,
            timeline,
            continuations: [],
          },
        });
      } else {
        merged.push(item);
      }
      continue;
    }

    const targetIndex = (() => {
      for (let index = merged.length - 1; index >= 0; index -= 1) {
        const candidate = merged[index];
        if (
          candidate.type === 'assistant_process'
          && (candidate.node_id === targetNodeId || candidate.anchor_node_id === targetNodeId)
        ) {
          return index;
        }
      }
      return -1;
    })();
    if (targetIndex < 0) {
      merged.push(item);
      continue;
    }

    const target = merged[targetIndex];
    const marker = getContinuationMarker(item);
    const continuationTimeline = getTimelineBlocks(item);
    merged[targetIndex] = {
      ...target,
      status: item.status || target.status,
      preview: item.preview || target.preview,
      props: {
        ...target.props,
        timeline: [
          ...getTimelineBlocks(target),
          ...(marker ? [{ type: 'marker', content: marker }] : []),
          ...continuationTimeline,
        ],
        continuation_run_ids: [
          ...(
            Array.isArray(target.props?.continuation_run_ids)
              ? target.props.continuation_run_ids.filter((value): value is string => typeof value === 'string')
              : []
          ),
          ...(item.run_id ? [item.run_id] : []),
        ],
      },
    };
  }
  return merged;
}

export function normalizeTranscriptItems(items: TranscriptItem[]): TranscriptItem[] {
  const normalized = items
    .filter((item) => !item.visibility || item.visibility === 'main')
    .map(normalizeTranscriptItem)
    .filter((item): item is TranscriptItem => Boolean(item));
  return filterStaleRunDraftItems(mergeContinuationProcessItems(normalized));
}

export interface LiveRunTranscriptOverlay {
  runId: string;
  nodeId?: string | null;
  targetNodeId?: string | null;
  anchorNodeId?: string | null;
  items: TranscriptItem[];
}

function itemBelongsToRun(item: TranscriptItem, runId: string): boolean {
  return item.run_id === runId;
}

function itemMatchesNode(item: TranscriptItem, nodeId: string | null | undefined): boolean {
  return Boolean(nodeId && (item.node_id === nodeId || item.anchor_node_id === nodeId));
}

function hasUserMessageForNode(items: TranscriptItem[], nodeId: string | null | undefined): boolean {
  return Boolean(nodeId && items.some((item) => item.type === 'user_message' && item.node_id === nodeId));
}

function suppressPendingBubble(items: TranscriptItem[]): TranscriptItem[] {
  return items.map((item) => {
    if (item.type !== 'assistant_process' || !item.props?.showPendingBubble) return item;
    return {
      ...item,
      props: {
        ...item.props,
        pendingUserMessage: null,
        showPendingBubble: false,
      },
    };
  });
}

function findLiveRunInsertionIndex(items: TranscriptItem[], overlay: LiveRunTranscriptOverlay): number {
  const existingRunIndex = items.findIndex((item) => itemBelongsToRun(item, overlay.runId));
  if (existingRunIndex >= 0) return existingRunIndex;

  const targetNodeId = overlay.targetNodeId || overlay.nodeId;
  if (targetNodeId) {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      if (itemMatchesNode(items[index], targetNodeId)) return index + 1;
    }
  }

  if (overlay.anchorNodeId) {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      if (itemMatchesNode(items[index], overlay.anchorNodeId)) return index + 1;
    }
  }

  return items.length;
}

export function mergeLiveRunTranscriptItems(
  baseItems: TranscriptItem[],
  liveRuns: LiveRunTranscriptOverlay[],
): TranscriptItem[] {
  let merged = normalizeTranscriptItems(baseItems);

  for (const liveRun of liveRuns) {
    const targetNodeId = liveRun.targetNodeId || liveRun.nodeId;
    const liveItems = hasUserMessageForNode(merged, targetNodeId)
      ? suppressPendingBubble(normalizeTranscriptItems(liveRun.items))
      : normalizeTranscriptItems(liveRun.items);
    if (liveItems.length === 0) continue;

    const insertionIndex = findLiveRunInsertionIndex(merged, liveRun);
    const removedBeforeInsertion = merged
      .slice(0, insertionIndex)
      .filter((item) => itemBelongsToRun(item, liveRun.runId))
      .length;
    const withoutStaleRunItems = merged.filter((item) => !itemBelongsToRun(item, liveRun.runId));
    const adjustedInsertionIndex = Math.min(
      Math.max(0, insertionIndex - removedBeforeInsertion),
      withoutStaleRunItems.length,
    );

    merged = mergeContinuationProcessItems([
      ...withoutStaleRunItems.slice(0, adjustedInsertionIndex),
      ...liveItems,
      ...withoutStaleRunItems.slice(adjustedInsertionIndex),
    ]);
  }

  return merged;
}
