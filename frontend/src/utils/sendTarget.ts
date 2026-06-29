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
