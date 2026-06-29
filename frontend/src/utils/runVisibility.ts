export interface BranchRunLike {
  kind: string;
  status: string;
  anchorNodeId: string | null;
  nodeId: string | null;
  targetNodeId: string | null;
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
  return targetNodeId === selectedBranchTipId || currentBranchNodeIds.has(targetNodeId);
}

export function isRunBlockingSelectedBranch(
  run: BranchRunLike,
  selectedBranchTipId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  return run.kind === 'chat'
    && run.status === 'streaming'
    && isRunVisibleInSelectedTranscript(run, selectedBranchTipId, currentBranchNodeIds);
}
