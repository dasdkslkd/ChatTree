export const SIDE_RUN_KINDS = new Set(['side_question', 'subagent', 'command', 'workflow', 'workflow_step', 'direct_response']);

export function isSideRunKind(kind: string): boolean {
  return SIDE_RUN_KINDS.has(kind);
}
