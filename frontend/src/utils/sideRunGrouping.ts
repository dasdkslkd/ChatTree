export const SIDE_RUN_GROUP_ORDER = ['side_question', 'subagent', 'terminal', 'workflow', 'direct_response'] as const;

export type SideRunGroupKind = typeof SIDE_RUN_GROUP_ORDER[number];

export interface SideRunLike {
  runId: string;
  kind: string;
  createdAt: number;
  parentRunId?: string | null;
  summary?: string;
  metadata?: Record<string, unknown>;
  workflowEvents?: WorkflowEventLike[];
}

export interface WorkflowEventLike {
  eventIndex: number;
  eventType: string;
  phase?: string | null;
  childRunId?: string | null;
  childKind?: string | null;
  status?: string | null;
  content?: string | null;
  payload?: unknown;
}

export interface SideRunDraftLike {
  run: SideRunLike;
}

export interface SideRunGroupItem<TDraft extends SideRunDraftLike> {
  draft: TDraft;
  run: TDraft['run'];
  steps: TDraft[];
}

export interface SideRunGroup<TDraft extends SideRunDraftLike> {
  kind: SideRunGroupKind;
  runs: Array<SideRunGroupItem<TDraft>>;
}

export interface WorkflowProgressStep {
  key: string;
  label: string;
  status: 'running' | 'completed' | 'error' | 'stopped' | 'pending';
  eventIndex: number;
  childRunId: string | null;
}

function isTopLevelSideRunKind(kind: string): kind is SideRunGroupKind {
  return (SIDE_RUN_GROUP_ORDER as readonly string[]).includes(kind);
}

function metadataNumber(metadata: Record<string, unknown> | undefined, keys: string[]): number | null {
  for (const key of keys) {
    const value = metadata?.[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim() !== '') {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
}

export function getWorkflowStepOrder(run: SideRunLike): number {
  return metadataNumber(run.metadata, [
    'workflow_step_index',
    'step_index',
    'step_order',
    'order',
    'index',
  ]) ?? Number.MAX_SAFE_INTEGER;
}

function compareByCreatedAtDesc<TDraft extends SideRunDraftLike>(a: TDraft, b: TDraft): number {
  return (b.run.createdAt || 0) - (a.run.createdAt || 0);
}

function compareWorkflowSteps<TDraft extends SideRunDraftLike>(a: TDraft, b: TDraft): number {
  const orderDelta = getWorkflowStepOrder(a.run) - getWorkflowStepOrder(b.run);
  if (orderDelta !== 0) return orderDelta;
  return (a.run.createdAt || 0) - (b.run.createdAt || 0);
}

function isWorkflowChildRun<TDraft extends SideRunDraftLike>(draft: TDraft, workflowRunIds: Set<string>): boolean {
  if (draft.run.kind === 'workflow_step') return Boolean(draft.run.parentRunId);
  return Boolean(draft.run.parentRunId && workflowRunIds.has(draft.run.parentRunId));
}

export function groupDetachedSideRuns<TDraft extends SideRunDraftLike>(drafts: TDraft[]): Array<SideRunGroup<TDraft>> {
  const workflowRunIds = new Set(drafts
    .filter((draft) => draft.run.kind === 'workflow')
    .map((draft) => draft.run.runId));
  const stepsByParent = new Map<string, TDraft[]>();
  for (const draft of drafts) {
    const parentRunId = draft.run.parentRunId;
    if (!parentRunId) continue;
    if (!isWorkflowChildRun(draft, workflowRunIds)) continue;
    const steps = stepsByParent.get(parentRunId) ?? [];
    steps.push(draft);
    stepsByParent.set(parentRunId, steps);
  }

  return SIDE_RUN_GROUP_ORDER
    .map((kind) => {
      const runs = drafts
        .filter((draft) => !isWorkflowChildRun(draft, workflowRunIds) && draft.run.kind === kind && isTopLevelSideRunKind(draft.run.kind))
        .sort(compareByCreatedAtDesc)
        .map((draft) => ({
          draft,
          run: draft.run,
          steps: [...(stepsByParent.get(draft.run.runId) ?? [])].sort(compareWorkflowSteps),
        }));
      return { kind, runs };
    })
    .filter((group) => group.runs.length > 0);
}

export function getWorkflowProgressSteps(run: SideRunLike): WorkflowProgressStep[] {
  const phases = new Map<string, WorkflowProgressStep>();
  const childRuns = new Map<string, WorkflowProgressStep>();
  for (const event of [...(run.workflowEvents || [])].sort((a, b) => a.eventIndex - b.eventIndex)) {
    if ((event.eventType === 'phase_start' || event.eventType === 'phase_end') && event.phase) {
      const existing = phases.get(event.phase);
      phases.set(event.phase, {
        key: `phase:${event.phase}`,
        label: event.phase,
        status: event.eventType === 'phase_end' ? 'completed' : (existing?.status === 'completed' ? 'completed' : 'running'),
        eventIndex: existing?.eventIndex ?? event.eventIndex,
        childRunId: null,
      });
    }
    if (event.eventType === 'workflow_child_event' && event.childRunId) {
      const payload = event.payload && typeof event.payload === 'object' ? event.payload as Record<string, unknown> : {};
      const payloadType = typeof payload.event_type === 'string' ? payload.event_type : '';
      const payloadStatus = typeof payload.status === 'string' ? payload.status : '';
      const existing = childRuns.get(event.childRunId);
      childRuns.set(event.childRunId, {
        key: `child:${event.childRunId}`,
        label: typeof payload.agent_name === 'string'
          ? payload.agent_name
          : `${event.childKind || 'child'} ${event.childRunId.slice(0, 8)}`,
        status: payloadType === 'subagent_error' || payloadStatus === 'failed'
          ? 'error'
          : payloadType === 'subagent_result' || payloadStatus === 'completed'
            ? 'completed'
            : existing?.status === 'completed'
              ? 'completed'
              : 'running',
        eventIndex: existing?.eventIndex ?? event.eventIndex,
        childRunId: event.childRunId,
      });
    }
  }
  const resultEvent = run.workflowEvents?.find((event) => event.eventType === 'workflow_result' || event.eventType === 'workflow_cancelled');
  const allSteps = [...phases.values(), ...childRuns.values()].sort((a, b) => a.eventIndex - b.eventIndex);
  if (allSteps.length === 0 && resultEvent) {
    return [{
      key: `result:${resultEvent.eventIndex}`,
      label: resultEvent.eventType === 'workflow_cancelled' ? '已取消' : '完成',
      status: resultEvent.eventType === 'workflow_cancelled' ? 'stopped' : 'completed',
      eventIndex: resultEvent.eventIndex,
      childRunId: null,
    }];
  }
  return allSteps;
}
