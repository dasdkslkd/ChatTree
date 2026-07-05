export interface BranchRunLike {
  kind: string;
  status: string;
  anchorNodeId: string | null;
  nodeId: string | null;
  targetNodeId: string | null;
  pendingUserMessage?: string | null;
  anchorUntilTargetLands?: boolean;
}

export function getRunTargetNodeId(run: BranchRunLike): string | null {
  return run.targetNodeId || run.nodeId || null;
}

export function isRunVisibleInSelectedTranscript(
  run: BranchRunLike,
  selectedBranchTipId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  const targetNodeId = getRunTargetNodeId(run);
  if (!targetNodeId) {
    return !run.anchorNodeId || run.anchorNodeId === selectedBranchTipId;
  }
  if (
    run.kind === 'chat'
    && (run.pendingUserMessage || run.anchorUntilTargetLands)
    && (run.status === 'streaming' || run.status === 'waiting_approval' || run.status === 'stopping')
    && (
      (run.anchorNodeId && run.anchorNodeId === selectedBranchTipId)
      || (!run.anchorNodeId && selectedBranchTipId === null && currentBranchNodeIds.size === 0)
    )
    && !currentBranchNodeIds.has(targetNodeId)
  ) {
    return true;
  }
  return targetNodeId === selectedBranchTipId || currentBranchNodeIds.has(targetNodeId);
}

export function isDetachedRunView(
  run: BranchRunLike,
  selectedBranchTipId: string | null,
): boolean {
  if (!['side_question', 'subagent', 'command', 'workflow', 'direct_response'].includes(run.kind)) return false;
  return !run.anchorNodeId || run.anchorNodeId === selectedBranchTipId;
}

export function isRunVisibleInMainTranscript(
  run: BranchRunLike,
  selectedBranchTipId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  if (run.kind === 'direct_response') return false;
  if (isDetachedRunView(run, selectedBranchTipId)) return false;
  return isRunVisibleInSelectedTranscript(run, selectedBranchTipId, currentBranchNodeIds);
}

export function shouldPatchRunIntoMainConversation(run: BranchRunLike): boolean {
  return run.kind === 'chat';
}

export function isRunBlockingSelectedBranch(
  run: BranchRunLike,
  selectedBranchTipId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  return run.kind === 'chat'
    && (run.status === 'streaming' || run.status === 'waiting_approval')
    && isRunVisibleInSelectedTranscript(run, selectedBranchTipId, currentBranchNodeIds);
}

export function isRunStoppableFromSelectedBranch(
  run: BranchRunLike,
  selectedBranchTipId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  if (run.status !== 'streaming' && run.status !== 'waiting_approval' && run.status !== 'stopping') return false;
  if (isRunBlockingSelectedBranch(run, selectedBranchTipId, currentBranchNodeIds)) return true;
  return isDetachedRunView(run, selectedBranchTipId);
}
