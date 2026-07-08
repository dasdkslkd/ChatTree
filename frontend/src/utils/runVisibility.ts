export interface BranchRunLike {
  runId?: string;
  kind: string;
  status: string;
  anchorNodeId: string | null;
  nodeId: string | null;
  targetNodeId: string | null;
  createdByRunId?: string | null;
  cancellationParentRunId?: string | null;
  pendingUserMessage?: string | null;
  anchorUntilTargetLands?: boolean;
}

export function getRunTargetNodeId(run: BranchRunLike): string | null {
  return run.targetNodeId || run.nodeId || null;
}

export function isRunAnchorVisibleOnSelectedBranch(
  anchorNodeId: string | null,
  selectedBranchTipId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  if (!anchorNodeId) return true;
  return anchorNodeId === selectedBranchTipId || currentBranchNodeIds.has(anchorNodeId);
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
    && (
      run.status === 'streaming'
      || run.status === 'waiting_approval'
      || run.status === 'stopping'
      || run.status === 'stopped'
      || run.status === 'error'
    )
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
  currentBranchNodeIds: Set<string>,
): boolean {
  if (!['side_question', 'subagent', 'command', 'workflow', 'workflow_step', 'direct_response'].includes(run.kind)) return false;
  return isRunAnchorVisibleOnSelectedBranch(run.anchorNodeId, selectedBranchTipId, currentBranchNodeIds);
}

export function isRunVisibleInMainTranscript(
  run: BranchRunLike,
  selectedBranchTipId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  if (run.kind === 'direct_response') return false;
  if (isDetachedRunView(run, selectedBranchTipId, currentBranchNodeIds)) return false;
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
  return isRunBlockingSelectedBranch(run, selectedBranchTipId, currentBranchNodeIds);
}

export function getStoppableRunIdsForSelectedBranch<T extends BranchRunLike & { runId: string }>(
  runs: T[],
  selectedBranchTipId: string | null,
  currentBranchNodeIds: Set<string>,
): string[] {
  const activeRuns = runs.filter((run) =>
    run.status === 'streaming' || run.status === 'waiting_approval' || run.status === 'stopping'
  );
  const stopIds = new Set(
    activeRuns
      .filter((run) => isRunStoppableFromSelectedBranch(run, selectedBranchTipId, currentBranchNodeIds))
      .map((run) => run.runId),
  );
  let changed = true;
  while (changed) {
    changed = false;
    for (const run of activeRuns) {
      if (stopIds.has(run.runId)) continue;
      if (run.cancellationParentRunId && stopIds.has(run.cancellationParentRunId)) {
        stopIds.add(run.runId);
        changed = true;
      }
    }
  }
  return activeRuns
    .filter((run) => stopIds.has(run.runId))
    .map((run) => run.runId);
}
