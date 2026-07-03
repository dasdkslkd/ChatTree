import type { RunRecord } from '../types/run';

export const SIDE_RUN_KINDS = new Set(['side_question', 'subagent', 'command', 'workflow', 'workflow_step', 'direct_response']);
export const COMMAND_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled']);

export function isSideRunKind(kind: string): boolean {
  return SIDE_RUN_KINDS.has(kind);
}

export function isCommandRunStatus(status: string): boolean {
  return COMMAND_RUN_STATUSES.has(status);
}

export function getVisibleSideRunRecords(
  runs: RunRecord[],
  hiddenSideRunIds: Set<string>,
): RunRecord[] {
  return runs.filter((run) =>
    isSideRunKind(run.kind)
    && !hiddenSideRunIds.has(run.run_id)
  );
}