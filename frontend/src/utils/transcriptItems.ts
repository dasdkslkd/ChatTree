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
  if (rawType === 'plan_card') return null;
  if (item.type) return item;
  const itemType = typeof item.item_type === 'string' && transcriptItemTypes.has(item.item_type)
    ? item.item_type
    : undefined;
  return itemType ? { ...item, type: itemType as TranscriptItem['type'] } : item;
}

export function normalizeTranscriptItems(items: TranscriptItem[]): TranscriptItem[] {
  return items
    .filter((item) => !item.visibility || item.visibility === 'main')
    .map(normalizeTranscriptItem)
    .filter((item): item is TranscriptItem => Boolean(item));
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

function findLiveRunInsertionIndex(items: TranscriptItem[], overlay: LiveRunTranscriptOverlay): number {
  const existingRunIndex = items.findIndex((item) => itemBelongsToRun(item, overlay.runId));
  if (existingRunIndex >= 0) return existingRunIndex;

  if (overlay.anchorNodeId) {
    const processIndex = items.findIndex((item) => {
      if (item.type !== 'assistant_process' || item.node_id !== overlay.anchorNodeId) return false;
      const timeline = Array.isArray(item.props?.timeline) ? item.props.timeline : [];
      return timeline.some((block) => {
        if (!block || typeof block !== 'object' || Array.isArray(block)) return false;
        return block.type === 'plan_proposal' && block.status === 'approved';
      });
    });
    if (processIndex >= 0) return processIndex + 1;
  }

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
    const liveItems = normalizeTranscriptItems(liveRun.items);
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

    merged = [
      ...withoutStaleRunItems.slice(0, adjustedInsertionIndex),
      ...liveItems,
      ...withoutStaleRunItems.slice(adjustedInsertionIndex),
    ];
  }

  return merged;
}
