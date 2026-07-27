export const SIDE_RUN_KINDS = new Set(['side_question', 'subagent', 'command', 'workflow', 'workflow_step', 'direct_response']);

export function isSideRunKind(kind: string): boolean {
  return SIDE_RUN_KINDS.has(kind);
}

export function createQueuedMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `queued-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
