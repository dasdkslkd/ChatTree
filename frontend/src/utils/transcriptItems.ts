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

export function normalizeTranscriptItems(items: TranscriptItem[]): TranscriptItem[] {
  const normalized = items
    .filter((item) => !item.visibility || item.visibility === 'main')
    .map(normalizeTranscriptItem)
    .filter((item): item is TranscriptItem => Boolean(item));
  return filterStaleRunDraftItems(normalized);
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
