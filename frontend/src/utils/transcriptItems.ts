import type { TranscriptItem, TranscriptPatch, TranscriptSnapshot, UserMessageItem } from '../types/transcript';

export interface TranscriptState {
  conversationId: string | null;
  nodeId: string | null;
  revision: number;
  items: TranscriptItem[];
}

export type TranscriptScrollTarget = {
  messageId?: string | null;
  nodeId?: string | null;
  legacyIndex?: number | null;
};

export function getTranscriptItemNodeId(item: TranscriptItem): string | null {
  return item.node_id || null;
}

export function getTranscriptItemMessageId(item: TranscriptItem): string | null {
  return 'message_id' in item ? item.message_id : null;
}

export function findTranscriptAnchorElement(
  container: HTMLElement | null,
  target: TranscriptScrollTarget,
): HTMLElement | null {
  const anchors = Array.from(
    container?.querySelectorAll<HTMLElement>('[data-transcript-message-id], [data-transcript-node-id]') ?? [],
  );
  if (target.messageId) {
    const byMessage = anchors.find((element) => element.dataset.transcriptMessageId === target.messageId);
    if (byMessage) return byMessage;
  }
  if (target.nodeId) {
    const byNode = anchors.find((element) => element.dataset.transcriptNodeId === target.nodeId);
    if (byNode) return byNode;
  }
  return target.legacyIndex === undefined || target.legacyIndex === null
    ? null
    : document.getElementById(`message-${target.legacyIndex}`);
}

export function isTranscriptItemVisibleNow(
  item: TranscriptItem,
  currentConversationId: string | null,
  selectedBranchTipId: string | null,
): boolean {
  if (!currentConversationId) return false;
  if (item.conversation_id && item.conversation_id !== currentConversationId) return false;
  const itemNodeId = getTranscriptItemNodeId(item);
  return !itemNodeId || itemNodeId === selectedBranchTipId;
}

export function isTranscriptItemOnCurrentBranch(
  item: TranscriptItem,
  currentConversationId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  if (!currentConversationId) return false;
  if (item.conversation_id && item.conversation_id !== currentConversationId) return false;
  const itemNodeId = getTranscriptItemNodeId(item);
  return Boolean(itemNodeId && currentBranchNodeIds.has(itemNodeId));
}

export function getEditableUserMessageAttachmentRefs(
  item: UserMessageItem,
): {
  importFiles: string[];
  imageRefs: Array<{ filename: string; mime_type?: string }>;
} {
  return {
    importFiles: (item.import_files ?? []).map((file) => file.filename).filter(Boolean),
    imageRefs: (item.image_refs ?? [])
      .filter((file) => Boolean(file.filename))
      .map((file) => ({ filename: file.filename, mime_type: file.mime_type ?? undefined })),
  };
}

export function userMessageItemReferencesAttachment(item: UserMessageItem, filename: string): boolean {
  return Boolean(
    item.import_files?.some((file) => file.filename === filename)
    || item.image_refs?.some((file) => file.filename === filename)
  );
}

export type TranscriptPatchResult =
  | { status: 'applied'; state: TranscriptState }
  | { status: 'ignored'; state: TranscriptState }
  | { status: 'snapshot_needed'; state: TranscriptState };

const TRANSCRIPT_ITEM_TYPES = new Set([
  'user_message',
  'assistant_process',
  'assistant_answer',
  'plan_question',
  'plan_approval',
  'tool_approval',
  'task_notification',
  'compact',
  'run_status',
]);

export function normalizeTranscriptItems(items: TranscriptItem[]): TranscriptItem[] {
  return items.filter((item) => (
    typeof item.id === 'string'
    && typeof item.type === 'string'
    && TRANSCRIPT_ITEM_TYPES.has(item.type)
  ));
}

export function stateFromTranscriptSnapshot(snapshot: TranscriptSnapshot): TranscriptState {
  return {
    conversationId: snapshot.conversation_id,
    nodeId: snapshot.node_id,
    revision: Number(snapshot.revision || 0),
    items: normalizeTranscriptItems(snapshot.items || []),
  };
}

export function applyTranscriptPatch(
  state: TranscriptState,
  patch: TranscriptPatch,
): TranscriptPatchResult {
  if (state.conversationId !== patch.conversation_id) {
    return { status: 'snapshot_needed', state };
  }

  if (state.nodeId !== patch.node_id) {
    return { status: 'snapshot_needed', state };
  }

  if (patch.revision <= state.revision) {
    return { status: 'ignored', state };
  }

  if (patch.revision !== state.revision + 1) {
    return { status: 'snapshot_needed', state };
  }

  let nextItems = [...state.items];
  for (const operation of patch.operations || []) {
    if (operation.op === 'remove') {
      nextItems = nextItems.filter((item) => item.id !== operation.id);
    }
  }
  const upserts: Array<{ item: TranscriptItem; index: number }> = [];
  for (const operation of patch.operations || []) {
    if (operation.op !== 'upsert') {
      continue;
    }
    const item = normalizeTranscriptItems([operation.item])[0];
    const index = Number(operation.index);
    if (!item || !Number.isInteger(index) || index < 0) {
      return { status: 'snapshot_needed', state };
    }
    upserts.push({ item, index });
  }
  upserts.sort((left, right) => left.index - right.index);
  const upsertIds = new Set(upserts.map(({ item }) => item.id));
  nextItems = nextItems.filter((item) => !upsertIds.has(item.id));
  for (const { item, index } of upserts) {
    nextItems.splice(Math.min(index, nextItems.length), 0, item);
  }

  return {
    status: 'applied',
    state: {
      ...state,
      revision: patch.revision,
      items: nextItems,
    },
  };
}
