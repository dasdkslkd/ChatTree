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

export function isDetachedRunView(
  run: BranchRunLike,
  selectedBranchTipId: string | null,
): boolean {
  const targetNodeId = getRunTargetNodeId(run);
  if (targetNodeId) return false;
  if (!['side_question', 'subagent', 'workflow', 'direct_response'].includes(run.kind)) return false;
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

export function isRunBlockingSelectedBranch(
  run: BranchRunLike,
  selectedBranchTipId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  return run.kind === 'chat'
    && run.status === 'streaming'
    && isRunVisibleInSelectedTranscript(run, selectedBranchTipId, currentBranchNodeIds);
}
