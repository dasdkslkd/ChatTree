import type {
  SendMessageRequest,
  ToolApprovalPayload,
} from '../types/message';
import type { PlanActionStreamRequest, PlanAnswerStreamRequest, PlanRejectStreamRequest } from '../api/message';
import type { RunEventPayload, RunRecord } from '../types/run';
import { messageApi } from '../api/message';
import { runsApi } from '../api/runs';
import { slashRegistry } from './slashRegistry';
import { isSideRunKind } from '../utils/sideRunSync';
import { flushPerfEvents } from '../perf/client';
import { perfNow, recordMark, recordSpan } from '../perf/marks';
import {
  composeConnectionAbortSignal,
  connectionEpochRuntime,
  type ConnectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';

export const STREAM_DURATION_UPDATE_MS = 1000;

export interface StreamState {
  epochToken: ConnectionEpochToken;
  runId: string;
  status: 'idle' | 'streaming' | 'waiting_approval' | 'stopping' | 'completed' | 'error' | 'stopped';
  content: string;
  reasoning: string;
  reasoningActive: boolean;
  toolInteractions: any[];
  pendingApprovals: Record<string, ToolApprovalPayload>;
  anchorNodeId: string | null;
  nodeId: string | null;
  targetNodeId: string | null;
  conversationId: string;
  kind: string;
  createdByRunId: string | null;
  cancellationParentRunId: string | null;
  summary: string;
  metadata: Record<string, unknown>;
  workflowEvents: WorkflowEventState[];
  command: CommandRunState;
  sideRunNotifications: SideRunNotificationState[];
  tokensUsed: number;
  duration: number;
  errorMessage: string | null;
  abortController: AbortController | null;
  pendingUserMessage: string | null;
  toolPermissionMode: string | null;
  taskContextMode: 'attached' | 'detached' | null;
  anchorUntilTargetLands: boolean;
  eventCount: number;
  createdAt: number;
}

export interface CommandRunState {
  stdout: string;
  stderr: string;
  events: CommandEventState[];
  exitCode: number | null;
  durationSeconds: number | null;
  status: string | null;
  pid: number | null;
  command: string | null;
  cwd: string | null;
}

export interface CommandEventState {
  eventIndex: number;
  eventType: string;
  channel: string | null;
  content: string | null;
  exitCode: number | null;
  durationSeconds: number | null;
  status: string | null;
  pid: number | null;
  command: string | null;
  cwd: string | null;
  error: string | null;
}

export interface WorkflowEventState {
  eventIndex: number;
  eventType: string;
  phase: string | null;
  childRunId: string | null;
  childKind: string | null;
  status: string | null;
  content: string | null;
  payload: unknown;
}

export interface SideRunNotificationState {
  runId: string;
  kind: string;
}

type StatusListener = (conversationId: string) => void;

interface FinishInfo {
  conversationId: string;
  runId: string;
  status: 'completed' | 'error' | 'stopped';
  drained: boolean;
  nodeId: string | null;
  targetNodeId: string | null;
  controller: AbortController;
}
type FinishListener = (info: FinishInfo) => void;

export interface ResumeStreamOptions {
  anchorUntilTargetLands?: boolean;
}

export type StreamEpochSource = Pick<
  ConnectionEpochRuntime,
  'capture' | 'isCurrent' | 'signalFor'
>;

type RunAlias = {
  runId: string;
  epochToken: ConnectionEpochToken;
};

function mergeToolCalls(existing: any[], incoming: any[]): any[] {
  const merged = incoming.length > 0
    ? existing.filter((toolCall) => !toolCall?.pending)
    : [...existing];
  for (const toolCall of incoming) {
    const key = toolCall?.id ?? toolCall?.index;
    const idx = key == null ? -1 : merged.findIndex((item) => (item?.id ?? item?.index) === key);
    if (idx >= 0) merged[idx] = { ...merged[idx], ...toolCall };
    else merged.push(toolCall);
  }
  return merged;
}

function normalizeToolRoundId(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
}

function getChunkToolRoundId(chunk: any): string | null {
  return normalizeToolRoundId(chunk?.tool_round_id)
    || normalizeToolRoundId(chunk?.tool_call?.tool_round_id)
    || (typeof chunk?.tool_round === 'number' ? `tool-round-${chunk.tool_round}` : null);
}

function getChunkToolRound(chunk: any): number | null {
  if (typeof chunk?.tool_round === 'number') return chunk.tool_round;
  if (typeof chunk?.tool_call?.tool_round === 'number') return chunk.tool_call.tool_round;
  return null;
}

function toolCallKey(toolCall: any, index: number): string {
  return String(toolCall?.id ?? toolCall?.tool_call_id ?? toolCall?.index ?? index);
}

function withToolRound(toolCall: any, roundId: string | null, round: number | null): any {
  if (!toolCall || typeof toolCall !== 'object') return toolCall;
  return {
    ...toolCall,
    ...(roundId ? { tool_round_id: roundId } : {}),
    ...(round != null ? { tool_round: round } : {}),
  };
}

function getChunkToolCalls(chunk: any): any[] {
  if (Array.isArray(chunk.tool_calls)) return chunk.tool_calls;
  if (Array.isArray(chunk.tool_call?.tool_calls)) return chunk.tool_call.tool_calls;
  if (
    chunk.tool_call
    && typeof chunk.tool_call === 'object'
    && (chunk.event_type === 'tool_call' || chunk.event_type === 'tool_call_start')
  ) return [chunk.tool_call];
  return [];
}

function findToolRoundIndex(toolInteractions: any[], roundId: string | null): number {
  if (!roundId) return -1;
  return toolInteractions.findIndex((interaction) => (
    interaction?.tool_round_id === roundId
    || interaction?.assistant?.tool_round_id === roundId
  ));
}

function isProvisionalToolInteraction(interaction: any): boolean {
  if (!interaction || typeof interaction !== 'object') return false;
  const hasRound = Boolean(
    normalizeToolRoundId(interaction.tool_round_id)
    || normalizeToolRoundId(interaction.assistant?.tool_round_id)
    || typeof interaction.tool_round === 'number'
    || typeof interaction.assistant?.tool_round === 'number',
  );
  return !hasRound
    && Array.isArray(interaction.assistant?.tool_calls)
    && interaction.assistant.tool_calls.length === 0
    && Array.isArray(interaction.tools)
    && interaction.tools.length === 0;
}

function createToolRoundInteraction(
  toolCalls: any[],
  content: string,
  reasoning: string,
  roundId: string | null,
  round: number | null,
): any {
  return {
    ...(roundId ? { tool_round_id: roundId } : {}),
    ...(round != null ? { tool_round: round } : {}),
    assistant: {
      role: 'assistant',
      content,
      tool_calls: toolCalls,
      ...(roundId ? { tool_round_id: roundId } : {}),
      ...(round != null ? { tool_round: round } : {}),
    },
    tools: [],
    reasoning: reasoning || null,
  };
}

function appendToolCalls(
  toolInteractions: any[],
  toolCalls: any[],
  content: string,
  reasoning: string,
  roundId: string | null,
  round: number | null,
  replaceSnapshot: boolean,
): any[] {
  if (toolCalls.length === 0) return toolInteractions;
  const normalizedCalls = toolCalls.map((toolCall) => withToolRound(toolCall, roundId, round));
  const next = [...toolInteractions];
  const roundIndex = findToolRoundIndex(next, roundId);
  const tail = next.at(-1);
  const provisionalTailIndex = replaceSnapshot
    && roundIndex < 0
    && isProvisionalToolInteraction(tail)
    ? next.length - 1
    : -1;
  const index = roundIndex >= 0 ? roundIndex : provisionalTailIndex;
  if (index >= 0) {
    const last = next[index];
    const existingCalls = Array.isArray(last?.assistant?.tool_calls) ? last.assistant.tool_calls : [];
    next[index] = {
      ...last,
      ...(roundId ? { tool_round_id: roundId } : {}),
      ...(round != null ? { tool_round: round } : {}),
      assistant: {
        ...last.assistant,
        content: content || last.assistant?.content || '',
        tool_calls: replaceSnapshot ? normalizedCalls : mergeToolCalls(existingCalls, normalizedCalls),
        ...(roundId ? { tool_round_id: roundId } : {}),
        ...(round != null ? { tool_round: round } : {}),
      },
      reasoning: reasoning || last.reasoning || null,
    };
    return next;
  }
  next.push(createToolRoundInteraction(normalizedCalls, content, reasoning, roundId, round));
  return next;
}

function appendToolResult(
  toolInteractions: any[],
  toolResult: any,
  roundId: string | null,
  round: number | null,
): any[] {
  if (!toolResult) return toolInteractions;
  const next = toolInteractions.length > 0
    ? toolInteractions.map((interaction) => ({
        ...interaction,
        tools: Array.isArray(interaction.tools) ? [...interaction.tools] : [],
      }))
    : [createToolRoundInteraction([], '', '', roundId, round)];
  const targetId = toolResult.tool_call_id;
  const roundIndex = findToolRoundIndex(next, roundId);
  const targetIndex = targetId
    ? next.findIndex((interaction) => (interaction.assistant?.tool_calls || []).some((call: any) => call?.id === targetId))
    : -1;
  const index = roundIndex >= 0 ? roundIndex : targetIndex >= 0 ? targetIndex : next.length - 1;
  next[index] = {
    ...next[index],
    ...(roundId ? { tool_round_id: roundId } : {}),
    ...(round != null ? { tool_round: round } : {}),
  };
  const tools = next[index].tools;
  const existingIndex = targetId ? tools.findIndex((tool: any) => tool?.tool_call_id === targetId) : -1;
  const normalizedResult = {
    role: 'tool',
    ...toolResult,
    ...(roundId ? { tool_round_id: roundId } : {}),
    ...(round != null ? { tool_round: round } : {}),
  };
  if (existingIndex >= 0) tools[existingIndex] = { ...tools[existingIndex], ...normalizedResult };
  else tools.push(normalizedResult);
  return next;
}

function formatToolProgress(progress: any): string {
  if (!progress || typeof progress !== 'object') return '';
  const parts: string[] = [];
  if (typeof progress.phase === 'string') parts.push(progress.phase);
  if (typeof progress.scanned_entries === 'number') parts.push(`scanned ${progress.scanned_entries}`);
  if (typeof progress.matched_entries === 'number') parts.push(`matched ${progress.matched_entries}`);
  if (typeof progress.matches === 'number') parts.push(`matches ${progress.matches}`);
  if (typeof progress.searched_files === 'number') parts.push(`files ${progress.searched_files}`);
  if (typeof progress.elapsed_ms === 'number' && progress.elapsed_ms > 0) parts.push(`${Math.round(progress.elapsed_ms)}ms`);
  return parts.join(' · ');
}

function appendToolProgress(toolInteractions: any[], toolCall: any, roundId: string | null, round: number | null): any[] {
  if (!toolCall) return toolInteractions;
  const progressText = formatToolProgress(toolCall.progress);
  return appendToolResult(toolInteractions, {
    role: 'tool',
    tool_call_id: toolCall.tool_call_id || toolCall.id,
    name: toolCall.name || toolCall.function?.name,
    status: toolCall.status || 'running',
    progress: toolCall.progress,
    content: progressText || 'running',
  }, roundId, round);
}

function appendToolResultDelta(toolInteractions: any[], toolCall: any, roundId: string | null, round: number | null): any[] {
  if (!toolCall) return toolInteractions;
  const targetId = toolCall.tool_call_id || toolCall.id;
  const delta = typeof toolCall.content_delta === 'string' ? toolCall.content_delta : '';
  if (!targetId || !delta) return appendToolProgress(toolInteractions, toolCall, roundId, round);
  const roundIndex = findToolRoundIndex(toolInteractions, roundId);
  const candidateTools = roundIndex >= 0
    ? toolInteractions[roundIndex]?.tools
    : toolInteractions.flatMap((interaction) => Array.isArray(interaction.tools) ? interaction.tools : []);
  const current = Array.isArray(candidateTools)
    ? candidateTools.find((tool) => tool?.tool_call_id === targetId)
    : null;
  const previousDelta = typeof current?.content_delta === 'string' ? current.content_delta : '';
  const contentDelta = `${previousDelta}${delta}`;
  return appendToolResult(toolInteractions, {
    role: 'tool',
    tool_call_id: targetId,
    name: toolCall.name || toolCall.function?.name,
    status: toolCall.status || 'running',
    content: contentDelta,
    content_delta: contentDelta,
  }, roundId, round);
}

function appendToolError(toolInteractions: any[], toolCall: any, roundId: string | null, round: number | null): any[] {
  if (!toolCall) return toolInteractions;
  const errorText = typeof toolCall.error === 'string'
    ? toolCall.error
    : JSON.stringify(toolCall.error ?? { error: 'tool failed' });
  return appendToolResult(toolInteractions, {
    role: 'tool',
    tool_call_id: toolCall.tool_call_id || toolCall.id,
    name: toolCall.name || toolCall.function?.name,
    status: 'error',
    error: toolCall.error || errorText,
    content: errorText,
  }, roundId, round);
}

function summarizeToolInteractions(toolInteractions: any[]): string {
  return JSON.stringify(toolInteractions.map((interaction) => ({
    round_id: interaction?.tool_round_id ?? interaction?.assistant?.tool_round_id ?? '',
    round: interaction?.tool_round ?? interaction?.assistant?.tool_round ?? '',
    calls: (interaction?.assistant?.tool_calls || []).map((call: any, index: number) => ({
      key: toolCallKey(call, index),
      status: call?.status ?? '',
      name: call?.function?.name ?? call?.name ?? '',
      args_len: String(call?.function?.arguments ?? call?.arguments ?? '').length,
    })),
    tools: (interaction?.tools || []).map((tool: any) => ({
      id: tool?.tool_call_id ?? '',
      status: tool?.status ?? '',
      result_id: tool?.tool_result_id ?? '',
      content_len: String(tool?.content ?? '').length,
      delta_len: String(tool?.content_delta ?? '').length,
    })),
  })));
}

function appendProcessContent(toolInteractions: any[], content: string): any[] {
  const next = [...toolInteractions];
  if (!isProvisionalToolInteraction(next.at(-1))) {
    next.push(createToolRoundInteraction([], '', '', null, null));
  }
  const index = next.length - 1;
  const interaction = next[index];
  next[index] = {
    ...interaction,
    assistant: {
      role: 'assistant',
      ...(interaction.assistant || {}),
      content: `${interaction.assistant?.content || ''}${content}`,
      tool_calls: Array.isArray(interaction.assistant?.tool_calls) ? interaction.assistant.tool_calls : [],
    },
  };
  return next;
}

function mergeApproval(
  pendingApprovals: Record<string, ToolApprovalPayload>,
  approval: ToolApprovalPayload | undefined,
  status?: ToolApprovalPayload['status'],
): Record<string, ToolApprovalPayload> {
  if (!approval?.id) return pendingApprovals;
  const existing = pendingApprovals[approval.id];
  return {
    ...pendingApprovals,
    [approval.id]: {
      ...existing,
      ...approval,
      status: status ?? approval.status ?? existing?.status,
    },
  };
}

function mapRunStatus(status: unknown): 'streaming' | 'waiting_approval' | 'stopping' | 'completed' | 'error' | 'stopped' | null {
  if (status === 'running' || status === 'content' || status === 'start') return 'streaming';
  if (status === 'waiting_approval') return 'waiting_approval';
  if (status === 'stopping') return 'stopping';
  if (status === 'complete' || status === 'completed') return 'completed';
  if (status === 'error' || status === 'failed') return 'error';
  if (status === 'stopped' || status === 'cancelled') return 'stopped';
  return null;
}

function mapRunRecordStatus(status: unknown): StreamState['status'] {
  if (status === 'waiting_approval') return 'waiting_approval';
  if (status === 'stopping') return 'stopping';
  if (status === 'completed') return 'completed';
  if (status === 'failed') return 'error';
  if (status === 'cancelled') return 'stopped';
  return 'streaming';
}

function createCommandState(): CommandRunState {
  return {
    stdout: '',
    stderr: '',
    events: [],
    exitCode: null,
    durationSeconds: null,
    status: null,
    pid: null,
    command: null,
    cwd: null,
  };
}

function getRequestRunKind(request: SendMessageRequest): string {
  const match = slashRegistry.match(request.content);
  return match?.command.run_kind || 'chat';
}

function normalizeTimestampMs(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value > 1_000_000_000_000 ? value : value * 1000;
}

function getStringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function normalizeToolPermissionMode(value: unknown): string | null {
  return value === 'auto_approve' || value === 'modify_only' || value === 'ask_always' || value === 'plan'
    ? value
    : null;
}

function normalizeTaskContextMode(value: unknown): 'attached' | 'detached' | null {
  return value === 'attached' || value === 'detached' ? value : null;
}

function isWorkflowEvent(chunk: any): boolean {
  return [
    'workflow_start',
    'workflow_log',
    'phase_start',
    'phase_end',
    'workflow_child_event',
    'workflow_result',
    'workflow_cancelled',
  ].includes(String(chunk?.event_type || ''));
}

function isCommandEvent(chunk: any): boolean {
  return typeof chunk.event_type === 'string' && chunk.event_type.startsWith('command_');
}

function toCommandEvent(chunk: any): CommandEventState {
  return {
    eventIndex: typeof chunk.event_index === 'number' ? chunk.event_index : -1,
    eventType: String(chunk.event_type || ''),
    channel: typeof chunk.channel === 'string' ? chunk.channel : null,
    content: typeof chunk.content === 'string' ? chunk.content : null,
    exitCode: typeof chunk.exit_code === 'number' ? chunk.exit_code : null,
    durationSeconds: typeof chunk.duration_seconds === 'number' ? chunk.duration_seconds : null,
    status: typeof chunk.command_status === 'string' ? chunk.command_status : null,
    pid: typeof chunk.pid === 'number' ? chunk.pid : null,
    command: typeof chunk.command === 'string' ? chunk.command : null,
    cwd: typeof chunk.cwd === 'string' ? chunk.cwd : null,
    error: typeof chunk.error === 'string' ? chunk.error : null,
  };
}

function applyCommandEvent(commandState: CommandRunState, chunk: any): CommandRunState {
  const event = toCommandEvent(chunk);
  const existingIndex = commandState.events.findIndex((item) => item.eventIndex === event.eventIndex && event.eventIndex >= 0);
  const events = existingIndex >= 0
    ? commandState.events.map((item, index) => (index === existingIndex ? event : item))
    : [...commandState.events, event].sort((a, b) => a.eventIndex - b.eventIndex);
  return {
    stdout: event.eventType === 'command_stdout' && event.content ? commandState.stdout + event.content : commandState.stdout,
    stderr: event.eventType === 'command_stderr' && event.content ? commandState.stderr + event.content : commandState.stderr,
    events,
    exitCode: event.exitCode ?? commandState.exitCode,
    durationSeconds: event.durationSeconds ?? commandState.durationSeconds,
    status: event.status
      ?? (event.eventType === 'command_exited' ? 'completed' : event.eventType === 'command_stopped' ? 'cancelled' : commandState.status),
    pid: event.pid ?? commandState.pid,
    command: event.command ?? commandState.command,
    cwd: event.cwd ?? commandState.cwd,
  };
}

function isAggregateResultEvent(chunk: any): boolean {
  return ['subagent_result', 'workflow_result'].includes(String(chunk?.event_type || ''));
}

function toWorkflowEvent(chunk: any): WorkflowEventState {
  return {
    eventIndex: typeof chunk.event_index === 'number' ? chunk.event_index : -1,
    eventType: String(chunk.event_type || ''),
    phase: getStringOrNull(chunk.phase),
    childRunId: getStringOrNull(chunk.child_run_id),
    childKind: getStringOrNull(chunk.child_kind),
    status: getStringOrNull(chunk.status),
    content: getStringOrNull(chunk.content),
    payload: chunk.payload,
  };
}

function toSideRunNotification(chunk: any): SideRunNotificationState | null {
  const runId = getStringOrNull(chunk.child_run_id);
  const kind = getStringOrNull(chunk.child_kind);
  if (!runId || !kind || !isSideRunKind(kind)) return null;
  return { runId, kind };
}

export class StreamManager {
  private streams = new Map<string, StreamState>();
  private runsByConversation = new Map<string, Set<string>>();
  private conversationSnapshots = new Map<string, { signature: string; states: StreamState[] }>();
  private listeners = new Set<StatusListener>();
  private finishListeners = new Set<FinishListener>();
  private durationTimers = new Map<string, number>();
  private pendingNotifyHandles = new Map<string, {
    handle: number;
    kind: 'animationFrame' | 'timeout';
    epochToken: ConnectionEpochToken;
  }>();
  private runAliases = new Map<string, RunAlias>();
  private tempSeq = 0;
  private readonly epochSource: StreamEpochSource;

  constructor(
    epochSource: StreamEpochSource = connectionEpochRuntime,
  ) {
    this.epochSource = epochSource;
  }

  private tokensMatch(
    left: ConnectionEpochToken,
    right: ConnectionEpochToken,
  ): boolean {
    return left.profileId === right.profileId
      && left.serverInstanceId === right.serverInstanceId
      && left.connectionEpoch === right.connectionEpoch
      && left.connectionLeaseId === right.connectionLeaseId
      && left.generation === right.generation;
  }

  private ownsState(
    state: StreamState | undefined,
    epochToken: ConnectionEpochToken,
  ): state is StreamState {
    return Boolean(
      state
      && this.tokensMatch(state.epochToken, epochToken),
    );
  }

  private ownsCurrentState(
    state: StreamState | undefined,
    epochToken: ConnectionEpochToken,
  ): state is StreamState {
    return this.ownsState(state, epochToken)
      && this.epochSource.isCurrent(epochToken);
  }

  private deleteOwnedAliasChain(
    rootRunId: string,
    epochToken: ConnectionEpochToken,
  ): void {
    let current = rootRunId;
    const seen = new Set<string>();
    while (!seen.has(current)) {
      seen.add(current);
      const alias = this.runAliases.get(current);
      if (!alias || !this.tokensMatch(alias.epochToken, epochToken)) return;
      if (this.runAliases.get(current) === alias) this.runAliases.delete(current);
      current = alias.runId;
    }
  }

  private resolveRunIdForToken(
    runId: string,
    epochToken: ConnectionEpochToken,
  ): string {
    let current = runId;
    const seen = new Set<string>();
    while (!seen.has(current)) {
      seen.add(current);
      const alias = this.runAliases.get(current);
      if (!alias || !this.tokensMatch(alias.epochToken, epochToken)) break;
      current = alias.runId;
    }
    return current;
  }

  private deleteAliasesEndingAt(
    runId: string,
    epochToken: ConnectionEpochToken,
  ): void {
    const ownedAliases = [...this.runAliases.entries()]
      .filter(([, alias]) => this.tokensMatch(alias.epochToken, epochToken))
      .filter(([source]) => this.resolveRunIdForToken(source, epochToken) === runId)
      .map(([source]) => source);
    for (const source of ownedAliases) this.runAliases.delete(source);
  }

  private cleanupOwnedState(
    runId: string,
    epochToken: ConnectionEpochToken,
    controller: AbortController | null,
    aliasRootRunId: string,
  ): boolean {
    this.deleteOwnedAliasChain(aliasRootRunId, epochToken);
    const state = this.streams.get(runId);
    if (!this.ownsState(state, epochToken)
        || state.abortController !== controller) return false;

    this.deleteAliasesEndingAt(runId, epochToken);
    const timer = this.durationTimers.get(runId);
    if (timer !== undefined) window.clearInterval(timer);
    this.durationTimers.delete(runId);
    state.abortController?.abort();
    this.streams.delete(runId);
    const set = this.runsByConversation.get(state.conversationId);
    set?.delete(runId);
    if (set && set.size === 0) this.runsByConversation.delete(state.conversationId);
    const snapshot = this.conversationSnapshots.get(state.conversationId);
    if (snapshot?.states.some((cached) => (
      cached.runId === runId
      && this.ownsState(cached, epochToken)
      && cached.abortController === controller
    ))) {
      this.conversationSnapshots.delete(state.conversationId);
    }
    const pending = this.pendingNotifyHandles.get(state.conversationId);
    if (pending
        && this.tokensMatch(pending.epochToken, epochToken)
        && !this.epochSource.isCurrent(epochToken)) {
      this.clearPendingNotify(state.conversationId);
    }
    return true;
  }

  getState(conversationId: string): Readonly<StreamState> | undefined {
    const states = this.getConversationStates(conversationId)
      .filter((state) => state.status === 'streaming' || state.status === 'waiting_approval' || state.status === 'stopped' || state.status === 'error');
    const streaming = states
      .filter((state) => state.status === 'streaming' || state.status === 'waiting_approval')
      .sort((a, b) => b.createdAt - a.createdAt)[0];
    if (streaming) return streaming;
    return states.sort((a, b) => b.createdAt - a.createdAt)[0];
  }

  getConversationStates(conversationId: string): StreamState[] {
    const ids = this.runsByConversation.get(conversationId);
    const states = ids ? [...ids]
      .map((id) => this.streams.get(id))
      .filter((state): state is StreamState => (
        Boolean(state && this.epochSource.isCurrent(state.epochToken))
      )) : [];
    const signature = states.map((state) => [
      state.runId,
      state.kind,
      state.status,
      state.createdByRunId ?? '',
      state.cancellationParentRunId ?? '',
      state.summary,
      JSON.stringify(state.metadata || {}),
      state.workflowEvents.length,
      state.sideRunNotifications.map((notification) => `${notification.runId}:${notification.kind}`).join(','),
      state.anchorNodeId ?? '',
      state.nodeId ?? '',
      state.targetNodeId ?? '',
      state.eventCount,
      state.duration,
      state.content,
      state.reasoning,
      Object.keys(state.pendingApprovals).length,
      summarizeToolInteractions(state.toolInteractions),
      state.errorMessage ?? '',
      state.pendingUserMessage ?? '',
      state.toolPermissionMode ?? '',
      state.anchorUntilTargetLands ? 'anchor' : '',
    ].join(':')).join('|');
    const cached = this.conversationSnapshots.get(conversationId);
    if (cached?.signature === signature) return cached.states;
    if (states.length > 0) {
      this.conversationSnapshots.set(conversationId, { signature, states });
    } else {
      this.conversationSnapshots.delete(conversationId);
    }
    return states;
  }

  isStreaming(conversationId?: string): boolean {
    const states = conversationId
      ? this.getConversationStates(conversationId)
      : [...this.streams.values()].filter((state) => (
          this.epochSource.isCurrent(state.epochToken)
        ));
    return states.some((state) => state.status === 'streaming' || state.status === 'waiting_approval' || state.status === 'stopping');
  }

  getStreamingConversationIds(): string[] {
    const ids: string[] = [];
    for (const [conversationId, runIds] of this.runsByConversation.entries()) {
      if ([...runIds].some((runId) => {
        const state = this.streams.get(runId);
        if (!state || !this.epochSource.isCurrent(state.epochToken)) return false;
        const status = state.status;
        return status === 'streaming' || status === 'waiting_approval' || status === 'stopping';
      })) {
        ids.push(conversationId);
      }
    }
    return ids;
  }

  subscribe(listener: StatusListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  onFinish(listener: FinishListener): () => void {
    this.finishListeners.add(listener);
    return () => this.finishListeners.delete(listener);
  }

  hasRun(runId: string): boolean {
    const state = this.streams.get(this.resolveRunId(runId));
    return Boolean(state && this.epochSource.isCurrent(state.epochToken));
  }

  private emitNotify(conversationId: string, epochToken: ConnectionEpochToken) {
    if (!this.epochSource.isCurrent(epochToken)) return;
    const started = perfNow();
    this.listeners.forEach((listener) => listener(conversationId));
    recordSpan('stream_manager.emit_notify', started, {
      conversation_id: conversationId,
      listener_count: this.listeners.size,
    });
  }

  private clearPendingNotify(conversationId: string) {
    const pending = this.pendingNotifyHandles.get(conversationId);
    if (!pending) return;
    if (pending.kind === 'animationFrame' && typeof window.cancelAnimationFrame === 'function') {
      window.cancelAnimationFrame(pending.handle);
    } else {
      window.clearTimeout(pending.handle);
    }
    this.pendingNotifyHandles.delete(conversationId);
  }

  private notify(
    conversationId: string,
    epochToken: ConnectionEpochToken,
    immediate = false,
  ) {
    if (!this.epochSource.isCurrent(epochToken)) return;
    recordMark('stream_manager.notify', {
      conversation_id: conversationId,
      immediate,
      pending: this.pendingNotifyHandles.has(conversationId),
    });
    if (immediate) {
      this.clearPendingNotify(conversationId);
      this.emitNotify(conversationId, epochToken);
      return;
    }
    const existing = this.pendingNotifyHandles.get(conversationId);
    if (existing && this.epochSource.isCurrent(existing.epochToken)) return;
    if (existing) this.clearPendingNotify(conversationId);
    if (typeof window.requestAnimationFrame === 'function') {
      const pending = {
        handle: 0,
        kind: 'animationFrame' as const,
        epochToken,
      };
      pending.handle = window.requestAnimationFrame(() => {
        if (this.pendingNotifyHandles.get(conversationId) !== pending) return;
        this.pendingNotifyHandles.delete(conversationId);
        this.emitNotify(conversationId, epochToken);
      });
      this.pendingNotifyHandles.set(conversationId, pending);
      return;
    }
    const pending = {
      handle: 0,
      kind: 'timeout' as const,
      epochToken,
    };
    pending.handle = window.setTimeout(() => {
      if (this.pendingNotifyHandles.get(conversationId) !== pending) return;
      this.pendingNotifyHandles.delete(conversationId);
      this.emitNotify(conversationId, epochToken);
    }, 50);
    this.pendingNotifyHandles.set(conversationId, pending);
  }

  private notifyFinish(info: FinishInfo, epochToken: ConnectionEpochToken) {
    if (!this.epochSource.isCurrent(epochToken)) return;
    this.finishListeners.forEach((listener) => listener(info));
  }

  private addToConversation(
    conversationId: string,
    runId: string,
    epochToken: ConnectionEpochToken,
  ) {
    if (!this.epochSource.isCurrent(epochToken)) return;
    const set = this.runsByConversation.get(conversationId) ?? new Set<string>();
    set.add(runId);
    this.runsByConversation.set(conversationId, set);
  }

  private replaceRunId(
    oldRunId: string,
    newRunId: string,
    epochToken: ConnectionEpochToken,
  ) {
    if (oldRunId === newRunId || !this.streams.has(oldRunId) || this.streams.has(newRunId)) return oldRunId;
    const state = this.streams.get(oldRunId)!;
    if (!this.ownsCurrentState(state, epochToken)) return oldRunId;
    this.streams.delete(oldRunId);
    this.streams.set(newRunId, { ...state, runId: newRunId });
    this.runAliases.set(oldRunId, { runId: newRunId, epochToken });
    const set = this.runsByConversation.get(state.conversationId);
    if (set) {
      set.delete(oldRunId);
      set.add(newRunId);
    }
    const timer = this.durationTimers.get(oldRunId);
    if (timer !== undefined) {
      this.durationTimers.delete(oldRunId);
      this.durationTimers.set(newRunId, timer);
    }
    return newRunId;
  }

  resolveRunId(runId: string): string {
    let current = runId;
    const seen = new Set<string>();
    while (!seen.has(current)) {
      seen.add(current);
      const alias = this.runAliases.get(current);
      if (!alias || !this.epochSource.isCurrent(alias.epochToken)) break;
      current = alias.runId;
    }
    return current;
  }

  areRunsInactive(runIds: string[]): boolean {
    return runIds.every((runId) => {
      const resolved = this.resolveRunId(runId);
      const state = this.streams.get(resolved);
      const status = state && this.epochSource.isCurrent(state.epochToken)
        ? state.status
        : undefined;
      return status !== 'streaming' && status !== 'waiting_approval' && status !== 'stopping';
    });
  }

  private applyChunk(
    runId: string,
    chunk: any,
    epochToken: ConnectionEpochToken,
  ): string {
    let state = this.streams.get(runId);
    if (!this.ownsCurrentState(state, epochToken)) return runId;
    const incomingRunId = chunk.run_id || chunk.runId;
    if (incomingRunId && incomingRunId !== runId) {
      runId = this.replaceRunId(runId, incomingRunId, epochToken);
      state = this.streams.get(runId);
      if (!this.ownsCurrentState(state, epochToken)) return runId;
    }

    let next: StreamState = { ...state };
    if (chunk.kind) {
      next.kind = String(chunk.kind);
    }
    if (chunk.created_by_run_id !== undefined) {
      next.createdByRunId = chunk.created_by_run_id || null;
    }
    if (chunk.cancellation_parent_run_id !== undefined) {
      next.cancellationParentRunId = chunk.cancellation_parent_run_id || null;
    }
    if (typeof chunk.summary === 'string') {
      next.summary = chunk.summary;
    }
    if (chunk.metadata && typeof chunk.metadata === 'object') {
      next.metadata = {
        ...next.metadata,
        ...chunk.metadata,
      };
    }
    const metadataPermissionMode = chunk.metadata && typeof chunk.metadata === 'object'
      ? normalizeToolPermissionMode((chunk.metadata as Record<string, unknown>).tool_permission_mode)
      : null;
    const chunkPermissionMode = normalizeToolPermissionMode(chunk.tool_permission_mode) ?? metadataPermissionMode;
    if (chunkPermissionMode) {
      next.toolPermissionMode = chunkPermissionMode;
      next.metadata = {
        ...next.metadata,
        tool_permission_mode: chunkPermissionMode,
      };
    }
    const metadataTaskContextMode = chunk.metadata && typeof chunk.metadata === 'object'
      ? normalizeTaskContextMode((chunk.metadata as Record<string, unknown>).task_context_mode)
      : null;
    const chunkTaskContextMode = normalizeTaskContextMode(chunk.task_context_mode) ?? metadataTaskContextMode;
    if (chunkTaskContextMode) {
      next.taskContextMode = chunkTaskContextMode;
      next.metadata = {
        ...next.metadata,
        task_context_mode: chunkTaskContextMode,
      };
    }
    const createdAt = normalizeTimestampMs(chunk.created_at);
    if (createdAt !== null) {
      next.createdAt = createdAt;
    }
    if (chunk.anchor_node_id !== undefined) {
      next.anchorNodeId = chunk.anchor_node_id || null;
    }
    if (chunk.node_id || chunk.target_node_id) {
      const nodeId = chunk.target_node_id || chunk.node_id;
      next.nodeId = chunk.node_id || nodeId;
      next.targetNodeId = nodeId;
    }
    if (typeof chunk.event_index === 'number') {
      next.eventCount = Math.max(next.eventCount, chunk.event_index + 1);
    }
    if (isWorkflowEvent(chunk)) {
      const workflowEvent = toWorkflowEvent(chunk);
      const existingIndex = next.workflowEvents.findIndex((event) => event.eventIndex === workflowEvent.eventIndex);
      next.workflowEvents = existingIndex >= 0
        ? next.workflowEvents.map((event, index) => (index === existingIndex ? workflowEvent : event))
        : [...next.workflowEvents, workflowEvent].sort((a, b) => a.eventIndex - b.eventIndex);
    }
    if (isCommandEvent(chunk)) {
      next.command = applyCommandEvent(next.command, chunk);
    }
    if (chunk.event_type === 'child_run_started') {
      const notification = toSideRunNotification(chunk);
      if (notification && !next.sideRunNotifications.some((item) => item.runId === notification.runId)) {
        next.sideRunNotifications = [...next.sideRunNotifications, notification];
      }
    }
    if (chunk.content && chunk.event_type === 'process_content') {
      next.toolInteractions = appendProcessContent(next.toolInteractions, chunk.content);
      next.reasoningActive = false;
    } else if (chunk.content && !isAggregateResultEvent(chunk) && !isCommandEvent(chunk)) {
      next.content += chunk.content;
      next.reasoningActive = false;
    }
    if (chunk.reasoning) {
      next.reasoning += chunk.reasoning;
      next.reasoningActive = true;
    }
    const toolRoundId = getChunkToolRoundId(chunk);
    const toolRound = getChunkToolRound(chunk);
    if (chunk.event_type === 'tool_calls_committed') {
      const toolCalls = getChunkToolCalls(chunk);
      next.toolInteractions = appendToolCalls(
        next.toolInteractions,
        toolCalls,
        next.content,
        next.reasoning,
        toolRoundId,
        toolRound,
        true,
      );
      if (toolCalls.length > 0) {
        next.content = '';
        next.reasoning = '';
        next.reasoningActive = false;
      }
    } else if (chunk.event_type === 'tool_call_start') {
      const toolCalls = getChunkToolCalls(chunk);
      if (toolCalls.length > 0) {
        next.toolInteractions = appendToolCalls(
          next.toolInteractions,
          toolCalls,
          next.content,
          next.reasoning,
          toolRoundId,
          toolRound,
          false,
        );
        next.content = '';
        next.reasoning = '';
      }
      next.reasoningActive = false;
    } else if (chunk.event_type === 'tool_call') {
      next.reasoningActive = false;
    } else if (chunk.event_type === 'tool_result') {
      next.toolInteractions = appendToolResult(next.toolInteractions, chunk.tool_call, toolRoundId, toolRound);
      next.reasoningActive = false;
    } else if (chunk.event_type === 'tool_progress') {
      next.toolInteractions = appendToolProgress(next.toolInteractions, chunk.tool_call, toolRoundId, toolRound);
      next.reasoningActive = false;
    } else if (chunk.event_type === 'tool_result_delta') {
      next.toolInteractions = appendToolResultDelta(next.toolInteractions, chunk.tool_call, toolRoundId, toolRound);
      next.reasoningActive = false;
    } else if (chunk.event_type === 'tool_call_error') {
      next.toolInteractions = appendToolError(next.toolInteractions, chunk.tool_call, toolRoundId, toolRound);
      next.reasoningActive = false;
    } else if (chunk.event_type === 'tool_approval_request') {
      next.pendingApprovals = mergeApproval(next.pendingApprovals, chunk.approval, 'pending');
    } else if (chunk.event_type === 'tool_approval_result') {
      next.pendingApprovals = mergeApproval(next.pendingApprovals, chunk.approval);
    }
    if (chunk.tokens_used) next.tokensUsed = chunk.tokens_used;
    const mappedStatus = mapRunStatus(chunk.status);
    if (mappedStatus === 'completed') {
      next.status = 'completed';
      next.reasoningActive = false;
    } else if (mappedStatus === 'stopped') {
      next.status = 'stopped';
      next.reasoningActive = false;
    } else if (mappedStatus === 'error') {
      next.status = 'error';
      next.errorMessage = typeof chunk.error === 'string' ? chunk.error : next.errorMessage;
      next.reasoningActive = false;
    } else if (mappedStatus === 'waiting_approval') {
      next.status = 'waiting_approval';
      next.reasoningActive = false;
    } else if (mappedStatus === 'stopping') {
      next.status = 'stopping';
      next.reasoningActive = false;
    } else if (mappedStatus === 'streaming' && next.status === 'waiting_approval') {
      next.status = 'streaming';
    }
    this.streams.set(runId, next);
    this.notify(
      next.conversationId,
      epochToken,
      next.status === 'completed'
        || next.status === 'stopped'
        || next.status === 'error'
        || chunk.event_type === 'tool_approval_request'
        || chunk.event_type === 'tool_approval_result',
    );
    return runId;
  }

  private createState(
    runId: string,
    epochToken: ConnectionEpochToken,
    conversationId: string,
    controller: AbortController,
    pendingUserMessage: string | null,
    nodeId: string | null,
    kind = 'chat',
    anchorNodeId?: string | null,
  ): StreamState {
    return {
      epochToken,
      runId,
      status: 'streaming',
      content: '',
      reasoning: '',
      reasoningActive: false,
      toolInteractions: [],
      pendingApprovals: {},
      anchorNodeId: anchorNodeId ?? nodeId,
      nodeId,
      targetNodeId: nodeId,
      conversationId,
      kind,
      createdByRunId: null,
      cancellationParentRunId: null,
      summary: '',
      metadata: {},
      workflowEvents: [],
      command: createCommandState(),
      sideRunNotifications: [],
      tokensUsed: 0,
      duration: 0,
      errorMessage: null,
      abortController: controller,
      pendingUserMessage,
      toolPermissionMode: null,
      taskContextMode: null,
      anchorUntilTargetLands: false,
      eventCount: 0,
      createdAt: Date.now(),
    };
  }

  restoreRunFromEvents(record: RunRecord, events: RunEventPayload[]): void {
    const epochToken = this.epochSource.capture();
    const initialRunId = record.run_id;
    let runId = initialRunId;
    const createdAt = Number.isFinite(record.created_at) ? record.created_at * 1000 : Date.now();
    const finishedAt = typeof record.finished_at === 'number' ? record.finished_at * 1000 : null;
    const duration = finishedAt !== null ? Math.max(0, finishedAt - createdAt) : 0;
    const initialState: StreamState = {
      epochToken,
      runId,
      status: mapRunRecordStatus(record.status),
      content: '',
      reasoning: '',
      reasoningActive: false,
      toolInteractions: [],
      pendingApprovals: {},
      anchorNodeId: record.anchor_node_id ?? null,
      nodeId: record.target_node_id ?? null,
      targetNodeId: record.target_node_id ?? null,
      conversationId: record.conversation_id,
      kind: record.kind,
      createdByRunId: record.created_by_run_id ?? null,
      cancellationParentRunId: record.cancellation_parent_run_id ?? null,
      summary: record.summary || '',
      metadata: { ...(record.metadata || {}) },
      workflowEvents: [],
      command: createCommandState(),
      sideRunNotifications: [],
      tokensUsed: 0,
      duration,
      errorMessage: typeof record.metadata?.error === 'string' ? record.metadata.error : null,
      abortController: null,
      pendingUserMessage: null,
      toolPermissionMode: normalizeToolPermissionMode(record.metadata?.tool_permission_mode),
      taskContextMode: normalizeTaskContextMode(record.metadata?.task_context_mode),
      anchorUntilTargetLands: false,
      eventCount: 0,
      createdAt,
    };
    if (!this.epochSource.isCurrent(epochToken)) return;
    this.streams.set(runId, initialState);
    this.addToConversation(record.conversation_id, runId, epochToken);
    const orderedEvents = [...events].sort((a, b) => (a.event_index ?? 0) - (b.event_index ?? 0));
    for (const event of orderedEvents) {
      if (event.event_type === 'tool_approval_request' || event.event_type === 'tool_approval_result') {
        continue;
      }
      if (!this.epochSource.isCurrent(epochToken)) {
        this.cleanupOwnedState(runId, epochToken, null, initialRunId);
        return;
      }
      runId = this.applyChunk(runId, event, epochToken);
    }
    const restored = this.streams.get(runId);
    if (!this.ownsCurrentState(restored, epochToken)) {
      this.cleanupOwnedState(runId, epochToken, null, initialRunId);
      return;
    }
    this.streams.set(runId, {
      ...restored,
      status: mapRunRecordStatus(record.status),
      eventCount: Math.max(restored.eventCount, record.event_count ?? restored.eventCount),
      duration: restored.duration || duration,
      errorMessage: restored.errorMessage ?? (typeof record.metadata?.error === 'string' ? record.metadata.error : null),
      abortController: null,
      reasoningActive: false,
      createdByRunId: record.created_by_run_id ?? restored.createdByRunId ?? null,
      cancellationParentRunId: record.cancellation_parent_run_id ?? restored.cancellationParentRunId ?? null,
      summary: record.summary || restored.summary || '',
      metadata: {
        ...(record.metadata || {}),
        ...(restored.metadata || {}),
      },
    });
    this.notify(record.conversation_id, epochToken, true);
  }

  async startStream(
    conversationId: string,
    request: SendMessageRequest,
    pendingUserMessage: string | null = null,
    requestNodeId?: string,
    anchorNodeId?: string | null,
  ): Promise<void> {
    const epochToken = this.epochSource.capture();
    let runId = `client_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    const state = this.createState(
      runId,
      epochToken,
      conversationId,
      abortController,
      pendingUserMessage,
      null,
      getRequestRunKind(request),
      anchorNodeId ?? requestNodeId ?? null,
    );
    state.taskContextMode = normalizeTaskContextMode(request.task_context_mode);
    if (!this.epochSource.isCurrent(epochToken)) return;
    this.streams.set(runId, state);
    this.addToConversation(conversationId, runId, epochToken);
    this.notify(conversationId, epochToken, true);
    const payload = {
      ...request,
      parent_node_id: requestNodeId ?? request.parent_node_id ?? null,
      focus_new_node: request.focus_new_node ?? true,
    };
    const signal = composeConnectionAbortSignal(
      abortController.signal,
      this.epochSource.signalFor(epochToken),
    );
    await this.consume(runId, epochToken, abortController, () => messageApi.stream(
      conversationId,
      payload,
      { token: epochToken, nodeId: requestNodeId, signal },
    ));
  }

  async startPlanApprovalStream(
    conversationId: string,
    planId: string,
    request: PlanActionStreamRequest = {},
    anchorNodeId?: string | null,
  ): Promise<void> {
    const epochToken = this.epochSource.capture();
    const runId = `client_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    const payload = { ...request, node_id: anchorNodeId ?? request.node_id ?? null };
    const state = this.createState(
      runId,
      epochToken,
      conversationId,
      abortController,
      null,
      null,
      'chat',
      anchorNodeId ?? request.node_id ?? null,
    );
    state.metadata = { origin: 'plan_approval', plan_id: planId };
    state.anchorUntilTargetLands = true;
    if (!this.epochSource.isCurrent(epochToken)) return;
    this.streams.set(runId, state);
    this.addToConversation(conversationId, runId, epochToken);
    this.notify(conversationId, epochToken, true);
    const signal = composeConnectionAbortSignal(
      abortController.signal,
      this.epochSource.signalFor(epochToken),
    );
    await this.consume(runId, epochToken, abortController, () => messageApi.streamPlanApproval(
      conversationId,
      planId,
      payload,
      { token: epochToken, signal },
    ));
  }

  async startPlanAnswerStream(
    conversationId: string,
    planId: string,
    answer: string,
    request: PlanActionStreamRequest = {},
    anchorNodeId?: string | null,
  ): Promise<void> {
    const epochToken = this.epochSource.capture();
    const runId = `client_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    const payload: PlanAnswerStreamRequest = {
      ...request,
      answer,
      node_id: anchorNodeId ?? request.node_id ?? null,
    };
    const state = this.createState(
      runId,
      epochToken,
      conversationId,
      abortController,
      null,
      null,
      'chat',
      anchorNodeId ?? request.node_id ?? null,
    );
    state.metadata = { origin: 'plan_question_answer', plan_id: planId };
    state.anchorUntilTargetLands = true;
    if (!this.epochSource.isCurrent(epochToken)) return;
    this.streams.set(runId, state);
    this.addToConversation(conversationId, runId, epochToken);
    this.notify(conversationId, epochToken, true);
    const signal = composeConnectionAbortSignal(
      abortController.signal,
      this.epochSource.signalFor(epochToken),
    );
    await this.consume(runId, epochToken, abortController, () => messageApi.streamPlanAnswer(
      conversationId,
      planId,
      payload,
      { token: epochToken, signal },
    ));
  }

  async startPlanRejectStream(
    conversationId: string,
    planId: string,
    request: PlanRejectStreamRequest,
    anchorNodeId?: string | null,
  ): Promise<void> {
    const epochToken = this.epochSource.capture();
    const runId = `client_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    const payload: PlanRejectStreamRequest = {
      ...request,
      node_id: anchorNodeId ?? request.node_id ?? null,
    };
    const state = this.createState(
      runId,
      epochToken,
      conversationId,
      abortController,
      null,
      null,
      'chat',
      anchorNodeId ?? request.node_id ?? null,
    );
    state.metadata = { origin: 'plan_reject', plan_id: planId };
    state.anchorUntilTargetLands = true;
    if (!this.epochSource.isCurrent(epochToken)) return;
    this.streams.set(runId, state);
    this.addToConversation(conversationId, runId, epochToken);
    this.notify(conversationId, epochToken, true);
    const signal = composeConnectionAbortSignal(
      abortController.signal,
      this.epochSource.signalFor(epochToken),
    );
    await this.consume(runId, epochToken, abortController, () => messageApi.streamPlanReject(
      conversationId,
      planId,
      payload,
      { token: epochToken, signal },
    ));
  }

  async resumeStream(
    conversationId: string,
    nodeId: string | null,
    runId?: string,
    fromEvent = 0,
    anchorNodeId?: string | null,
    kind = 'chat',
    options: ResumeStreamOptions = {},
  ): Promise<void> {
    const epochToken = this.epochSource.capture();
    const existing = this.getConversationStates(conversationId)
      .find((state) => this.ownsCurrentState(state, epochToken)
        && ((runId && state.runId === runId) || (nodeId && state.targetNodeId === nodeId)));
    if (existing?.status === 'streaming' || existing?.status === 'waiting_approval') return;
    if (!runId && !nodeId) return;
    const resolvedRunId = runId || `attach_${nodeId}`;
    const abortController = new AbortController();
    const state = this.createState(
      resolvedRunId,
      epochToken,
      conversationId,
      abortController,
      null,
      nodeId,
      kind,
      anchorNodeId,
    );
    state.anchorUntilTargetLands = options.anchorUntilTargetLands ?? false;
    if (!this.epochSource.isCurrent(epochToken)) return;
    this.streams.set(resolvedRunId, state);
    this.addToConversation(conversationId, resolvedRunId, epochToken);
    this.notify(conversationId, epochToken, true);
    const signal = composeConnectionAbortSignal(
      abortController.signal,
      this.epochSource.signalFor(epochToken),
    );
    await this.consume(resolvedRunId, epochToken, abortController, () => runId
      ? runsApi.attach(runId, { token: epochToken, fromEvent, signal })
      : messageApi.attachStream(
          conversationId,
          nodeId as string,
          { token: epochToken, fromEvent, signal },
        ));
  }

  private async consume(
    initialRunId: string,
    epochToken: ConnectionEpochToken,
    controller: AbortController,
    openStream: () => AsyncGenerator<any, void>,
  ): Promise<void> {
    let runId = initialRunId;
    let drained = false;
    let finishStatus: 'completed' | 'error' | 'stopped' = 'completed';
    const start = Date.now();
    const timer = window.setInterval(() => {
      const state = this.streams.get(runId);
      if (this.ownsCurrentState(state, epochToken)
          && state.abortController === controller
          && state.status === 'streaming') {
        this.streams.set(runId, { ...state, duration: Date.now() - start });
        this.notify(state.conversationId, epochToken);
      }
    }, STREAM_DURATION_UPDATE_MS);
    if (this.epochSource.isCurrent(epochToken)) {
      this.durationTimers.set(runId, timer);
    }

    try {
      for await (const chunk of openStream()) {
        if (!this.epochSource.isCurrent(epochToken)) break;
        const ownedState = this.streams.get(runId);
        if (!this.ownsCurrentState(ownedState, epochToken)
            || ownedState.abortController !== controller) break;
        const applyStarted = perfNow();
        runId = this.applyChunk(runId, chunk, epochToken);
        recordSpan('stream_manager.apply_chunk', applyStarted, {
          run_id: runId,
          status: chunk?.status,
          event_type: chunk?.event_type,
          event_index: chunk?.event_index,
        });
        const state = this.streams.get(runId);
        const mappedStatus = mapRunStatus(chunk.status);
        if (mappedStatus === 'error') finishStatus = 'error';
        else if (mappedStatus === 'stopped') finishStatus = 'stopped';
        else if (mappedStatus === 'completed') finishStatus = 'completed';
        if (!this.ownsCurrentState(state, epochToken)
            || state.abortController !== controller
            || state.abortController.signal.aborted) break;
      }
      drained = true;
    } catch (err) {
      finishStatus = err instanceof Error && err.name === 'AbortError' ? 'stopped' : 'error';
      const state = this.streams.get(runId);
      if (this.ownsCurrentState(state, epochToken)
          && state.abortController === controller) {
        this.streams.set(runId, {
          ...state,
          status: finishStatus === 'error' ? 'error' : 'stopped',
          errorMessage: finishStatus === 'error' && err instanceof Error ? err.message : state.errorMessage,
          reasoningActive: false,
        });
        this.notify(state.conversationId, epochToken, true);
      }
    } finally {
      window.clearInterval(timer);
      if (this.durationTimers.get(runId) === timer) {
        this.durationTimers.delete(runId);
      }
      const state = this.streams.get(runId);
      const ownsCurrent = this.ownsCurrentState(state, epochToken)
        && state.abortController === controller;
      if (!ownsCurrent) controller.abort();
      if (!this.epochSource.isCurrent(epochToken)) {
        this.cleanupOwnedState(
          runId,
          epochToken,
          controller,
          initialRunId,
        );
      } else if (ownsCurrent) {
        const finalStatus = state.status === 'streaming'
          ? finishStatus
          : state.status === 'stopping'
            ? 'stopped'
            : state.status;
        const finalState = {
          ...state,
          status: finalStatus,
          duration: Date.now() - start,
          reasoningActive: false,
        };
        this.streams.set(runId, finalState);
        this.notify(finalState.conversationId, epochToken, true);
        this.notifyFinish({
          conversationId: finalState.conversationId,
          runId,
          status: finalState.status === 'error' ? 'error' : finalState.status === 'stopped' ? 'stopped' : 'completed',
          drained,
          nodeId: finalState.nodeId,
          targetNodeId: finalState.targetNodeId,
          controller: finalState.abortController!,
        }, epochToken);
        void flushPerfEvents();
      }
    }
  }

  async stopRun(runId: string): Promise<void> {
    const state = this.streams.get(runId);
    if (!state || (state.status !== 'streaming' && state.status !== 'waiting_approval' && state.status !== 'stopping')) return;
    if (!this.epochSource.isCurrent(state.epochToken)) {
      this.cleanupOwnedState(
        runId,
        state.epochToken,
        state.abortController,
        runId,
      );
      return;
    }
    this.streams.set(runId, { ...state, status: 'stopping', reasoningActive: false });
    this.notify(state.conversationId, state.epochToken, true);
    const stopRequest = !runId.startsWith('client_') && !runId.startsWith('attach_')
      ? runsApi.stop(runId)
      : state.targetNodeId
        ? messageApi.stopStream(state.conversationId, state.targetNodeId)
        : Promise.resolve();
    state.abortController?.abort();
    try {
      await stopRequest;
    } catch (_) {
      state.abortController?.abort();
    }
  }

  async stopStream(conversationId: string): Promise<void> {
    const latest = this.getState(conversationId);
    if (latest) await this.stopRun(latest.runId);
  }

  cleanup(conversationId: string): void {
    for (const state of this.getConversationStates(conversationId)) {
      this.cleanupRun(state.runId);
    }
  }

  cleanupRun(runId: string): void {
    const state = this.streams.get(runId);
    if (!state) return;
    const timer = this.durationTimers.get(runId);
    if (timer !== undefined) window.clearInterval(timer);
    this.durationTimers.delete(runId);
    state.abortController?.abort();
    this.deleteAliasesEndingAt(runId, state.epochToken);
    this.streams.delete(runId);
    const set = this.runsByConversation.get(state.conversationId);
    set?.delete(runId);
    if (set && set.size === 0) this.runsByConversation.delete(state.conversationId);
    this.conversationSnapshots.delete(state.conversationId);
    const pending = this.pendingNotifyHandles.get(state.conversationId);
    if (pending
        && this.tokensMatch(pending.epochToken, state.epochToken)
        && !this.epochSource.isCurrent(state.epochToken)) {
      this.clearPendingNotify(state.conversationId);
    }
    this.notify(state.conversationId, state.epochToken, true);
  }

  archiveRun(runId: string): StreamState | null {
    const resolvedRunId = this.resolveRunId(runId);
    const state = this.streams.get(resolvedRunId);
    if (!state) return null;
    if (!this.epochSource.isCurrent(state.epochToken)) {
      this.cleanupRun(resolvedRunId);
      return null;
    }
    const archived: StreamState = {
      ...state,
      runId: resolvedRunId,
      abortController: null,
      reasoningActive: false,
    };
    this.cleanupRun(resolvedRunId);
    return archived;
  }

  cleanupIfController(conversationId: string, controller: AbortController, runId?: string): void {
    const states = runId ? [this.streams.get(runId)] : this.getConversationStates(conversationId);
    for (const state of states) {
      if (state?.abortController === controller) {
        this.cleanupRun(state.runId);
      }
    }
  }

  resetAll(): void {
    for (const state of this.streams.values()) {
      state.abortController?.abort();
    }
    for (const timer of this.durationTimers.values()) window.clearInterval(timer);
    for (const conversationId of this.pendingNotifyHandles.keys()) {
      this.clearPendingNotify(conversationId);
    }
    this.streams.clear();
    this.runsByConversation.clear();
    this.conversationSnapshots.clear();
    this.runAliases.clear();
    this.listeners.clear();
    this.finishListeners.clear();
    this.durationTimers.clear();
    this.pendingNotifyHandles.clear();
  }
}

export const streamManager = new StreamManager();
