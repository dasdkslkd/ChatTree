import type { SlashStreamTargetPolicy } from '../types/slash';

export interface SendNodeTargetInput {
  editTargetNodeId?: string | null;
  currentNodeId?: string | null;
  conversationCurrentNodeId?: string | null;
}

export function resolveSendNodeId({
  editTargetNodeId,
  currentNodeId,
  conversationCurrentNodeId,
}: SendNodeTargetInput): string | undefined {
  return editTargetNodeId || currentNodeId || conversationCurrentNodeId || undefined;
}

export function shouldDetachSlashStreamTarget(
  streamTargetPolicy?: SlashStreamTargetPolicy | null,
): boolean {
  return streamTargetPolicy === 'anchor_only' || streamTargetPolicy === 'none';
}

export function shouldSendSlashAnchorNode(
  streamTargetPolicy?: SlashStreamTargetPolicy | null,
): boolean {
  return shouldDetachSlashStreamTarget(streamTargetPolicy);
}

export function resolveSlashStreamNodeId({
  sendNodeId,
  streamTargetPolicy,
}: {
  sendNodeId?: string | null;
  streamTargetPolicy?: SlashStreamTargetPolicy | null;
}): string | undefined {
  return shouldDetachSlashStreamTarget(streamTargetPolicy)
    ? undefined
    : sendNodeId || undefined;
}
