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

export const STREAM_DURATION_UPDATE_MS = 1000;

export interface StreamState {
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

function getChunkToolCalls(chunk: any): any[] {
  if (Array.isArray(chunk.tool_calls)) return chunk.tool_calls;
  if (Array.isArray(chunk.tool_call?.tool_calls)) return chunk.tool_call.tool_calls;
  if (chunk.tool_call && typeof chunk.tool_call === 'object' && chunk.event_type === 'tool_call') return [chunk.tool_call];
  return [];
}

function appendToolCallStart(toolInteractions: any[], content: string, reasoning: string): any[] {
  const next = [...toolInteractions];
  next.push({
    assistant: {
      role: 'assistant',
      content,
      tool_calls: [{
        id: '__pending_tool_call__',
        type: 'function',
        pending: true,
        function: { name: '准备工具调用', arguments: '' },
      }],
    },
    tools: [],
    reasoning: reasoning || null,
  });
  return next;
}

function appendToolCalls(toolInteractions: any[], toolCalls: any[], content: string, reasoning: string): any[] {
  if (toolCalls.length === 0) return toolInteractions;
  const next = [...toolInteractions];
  const last = next[next.length - 1];
  if (last && Array.isArray(last?.assistant?.tool_calls) && (!Array.isArray(last.tools) || last.tools.length === 0)) {
    next[next.length - 1] = {
      ...last,
      assistant: {
        ...last.assistant,
        content: content || last.assistant?.content || '',
        tool_calls: mergeToolCalls(last.assistant.tool_calls, toolCalls),
      },
      reasoning: reasoning || last.reasoning || null,
    };
    return next;
  }
  next.push({
    assistant: { role: 'assistant', content, tool_calls: toolCalls },
    tools: [],
    reasoning: reasoning || null,
  });
  return next;
}

function appendToolResult(toolInteractions: any[], toolResult: any): any[] {
  if (!toolResult) return toolInteractions;
  const next = toolInteractions.length > 0
    ? toolInteractions.map((interaction) => ({
        ...interaction,
        tools: Array.isArray(interaction.tools) ? [...interaction.tools] : [],
      }))
    : [{ assistant: { role: 'assistant', content: '', tool_calls: [] }, tools: [], reasoning: null }];
  const targetId = toolResult.tool_call_id;
  const targetIndex = targetId
    ? next.findIndex((interaction) => (interaction.assistant?.tool_calls || []).some((call: any) => call?.id === targetId))
    : -1;
  const index = targetIndex >= 0 ? targetIndex : next.length - 1;
  const tools = next[index].tools;
  const existingIndex = targetId ? tools.findIndex((tool: any) => tool?.tool_call_id === targetId) : -1;
  if (existingIndex >= 0) tools[existingIndex] = { ...tools[existingIndex], ...toolResult };
  else tools.push({ role: 'tool', ...toolResult });
  return next;
}

function appendProcessContent(toolInteractions: any[], content: string): any[] {
  const next = toolInteractions.length > 0
    ? [...toolInteractions]
    : [{ assistant: { role: 'assistant', content: '', tool_calls: [] }, tools: [], reasoning: null }];
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
  private pendingNotifyHandles = new Map<string, { handle: number; kind: 'animationFrame' | 'timeout' }>();
  private runAliases = new Map<string, string>();
  private tempSeq = 0;

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
      .filter((state): state is StreamState => Boolean(state)) : [];
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
      state.toolInteractions.length,
      state.errorMessage ?? '',
      state.pendingUserMessage ?? '',
      state.toolPermissionMode ?? '',
      state.anchorUntilTargetLands ? 'anchor' : '',
    ].join(':')).join('|');
    const cached = this.conversationSnapshots.get(conversationId);
    if (cached?.signature === signature) return cached.states;
    this.conversationSnapshots.set(conversationId, { signature, states });
    return states;
  }

  isStreaming(conversationId?: string): boolean {
    const states = conversationId ? this.getConversationStates(conversationId) : [...this.streams.values()];
    return states.some((state) => state.status === 'streaming' || state.status === 'waiting_approval' || state.status === 'stopping');
  }

  getStreamingConversationIds(): string[] {
    const ids: string[] = [];
    for (const [conversationId, runIds] of this.runsByConversation.entries()) {
      if ([...runIds].some((runId) => {
        const status = this.streams.get(runId)?.status;
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
    return this.streams.has(this.resolveRunId(runId));
  }

  private emitNotify(conversationId: string) {
    this.listeners.forEach((listener) => listener(conversationId));
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

  private notify(conversationId: string, immediate = false) {
    if (immediate) {
      this.clearPendingNotify(conversationId);
      this.emitNotify(conversationId);
      return;
    }
    if (this.pendingNotifyHandles.has(conversationId)) return;
    if (typeof window.requestAnimationFrame === 'function') {
      const handle = window.requestAnimationFrame(() => {
        this.pendingNotifyHandles.delete(conversationId);
        this.emitNotify(conversationId);
      });
      this.pendingNotifyHandles.set(conversationId, { handle, kind: 'animationFrame' });
      return;
    }
    const handle = window.setTimeout(() => {
      this.pendingNotifyHandles.delete(conversationId);
      this.emitNotify(conversationId);
    }, 50);
    this.pendingNotifyHandles.set(conversationId, { handle, kind: 'timeout' });
  }

  private notifyFinish(info: FinishInfo) {
    this.finishListeners.forEach((listener) => listener(info));
  }

  private addToConversation(conversationId: string, runId: string) {
    const set = this.runsByConversation.get(conversationId) ?? new Set<string>();
    set.add(runId);
    this.runsByConversation.set(conversationId, set);
  }

  private replaceRunId(oldRunId: string, newRunId: string) {
    if (oldRunId === newRunId || !this.streams.has(oldRunId) || this.streams.has(newRunId)) return oldRunId;
    const state = this.streams.get(oldRunId)!;
    this.streams.delete(oldRunId);
    this.streams.set(newRunId, { ...state, runId: newRunId });
    this.runAliases.set(oldRunId, newRunId);
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
    while (this.runAliases.has(current) && !seen.has(current)) {
      seen.add(current);
      current = this.runAliases.get(current)!;
    }
    return current;
  }

  areRunsInactive(runIds: string[]): boolean {
    return runIds.every((runId) => {
      const resolved = this.resolveRunId(runId);
      const status = this.streams.get(resolved)?.status;
      return status !== 'streaming' && status !== 'waiting_approval' && status !== 'stopping';
    });
  }

  private applyChunk(runId: string, chunk: any): string {
    let state = this.streams.get(runId);
    if (!state) return runId;
    const incomingRunId = chunk.run_id || chunk.runId;
    if (incomingRunId && incomingRunId !== runId) {
      runId = this.replaceRunId(runId, incomingRunId);
      state = this.streams.get(runId);
      if (!state) return runId;
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
    if (chunk.event_type === 'tool_call_start') {
      next.toolInteractions = appendToolCallStart(next.toolInteractions, next.content, next.reasoning);
      next.content = '';
      next.reasoning = '';
      next.reasoningActive = false;
    } else if (chunk.event_type === 'tool_call') {
      const toolCalls = getChunkToolCalls(chunk);
      next.toolInteractions = appendToolCalls(next.toolInteractions, toolCalls, next.content, next.reasoning);
      if (toolCalls.length > 0) {
        next.content = '';
        next.reasoning = '';
        next.reasoningActive = false;
      }
    } else if (chunk.event_type === 'tool_result') {
      next.toolInteractions = appendToolResult(next.toolInteractions, chunk.tool_call);
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
    conversationId: string,
    controller: AbortController,
    pendingUserMessage: string | null,
    nodeId: string | null,
    kind = 'chat',
    anchorNodeId?: string | null,
  ): StreamState {
    return {
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
    const runId = record.run_id;
    const createdAt = Number.isFinite(record.created_at) ? record.created_at * 1000 : Date.now();
    const finishedAt = typeof record.finished_at === 'number' ? record.finished_at * 1000 : null;
    const duration = finishedAt !== null ? Math.max(0, finishedAt - createdAt) : 0;
    const initialState: StreamState = {
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
    this.streams.set(runId, initialState);
    this.addToConversation(record.conversation_id, runId);
    const orderedEvents = [...events].sort((a, b) => (a.event_index ?? 0) - (b.event_index ?? 0));
    for (const event of orderedEvents) {
      if (event.event_type === 'tool_approval_request' || event.event_type === 'tool_approval_result') {
        continue;
      }
      this.applyChunk(runId, event);
    }
    const restored = this.streams.get(runId);
    if (restored) {
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
    }
    this.notify(record.conversation_id, true);
  }

  async startStream(
    conversationId: string,
    request: SendMessageRequest,
    pendingUserMessage: string | null = null,
    requestNodeId?: string,
    anchorNodeId?: string | null,
  ): Promise<void> {
    let runId = `client_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    const state = this.createState(
      runId,
      conversationId,
      abortController,
      pendingUserMessage,
      null,
      getRequestRunKind(request),
      anchorNodeId ?? requestNodeId ?? null,
    );
    state.taskContextMode = normalizeTaskContextMode(request.task_context_mode);
    this.streams.set(runId, state);
    this.addToConversation(conversationId, runId);
    this.notify(conversationId, true);
    const payload = {
      ...request,
      parent_node_id: requestNodeId ?? request.parent_node_id ?? null,
      focus_new_node: request.focus_new_node ?? true,
    };
    await this.consume(runId, () => messageApi.stream(conversationId, payload, requestNodeId, abortController.signal));
  }

  async startPlanApprovalStream(
    conversationId: string,
    planId: string,
    request: PlanActionStreamRequest = {},
    anchorNodeId?: string | null,
  ): Promise<void> {
    const runId = `client_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    const payload = { ...request, node_id: anchorNodeId ?? request.node_id ?? null };
    const state = this.createState(
      runId,
      conversationId,
      abortController,
      null,
      null,
      'chat',
      anchorNodeId ?? request.node_id ?? null,
    );
    state.metadata = { origin: 'plan_approval', plan_id: planId };
    state.anchorUntilTargetLands = true;
    this.streams.set(runId, state);
    this.addToConversation(conversationId, runId);
    this.notify(conversationId, true);
    await this.consume(runId, () => messageApi.streamPlanApproval(conversationId, planId, payload, abortController.signal));
  }

  async startPlanAnswerStream(
    conversationId: string,
    planId: string,
    answer: string,
    request: PlanActionStreamRequest = {},
    anchorNodeId?: string | null,
  ): Promise<void> {
    const runId = `client_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    const payload: PlanAnswerStreamRequest = {
      ...request,
      answer,
      node_id: anchorNodeId ?? request.node_id ?? null,
    };
    const state = this.createState(
      runId,
      conversationId,
      abortController,
      null,
      null,
      'chat',
      anchorNodeId ?? request.node_id ?? null,
    );
    state.metadata = { origin: 'plan_question_answer', plan_id: planId };
    state.anchorUntilTargetLands = true;
    this.streams.set(runId, state);
    this.addToConversation(conversationId, runId);
    this.notify(conversationId, true);
    await this.consume(runId, () => messageApi.streamPlanAnswer(conversationId, planId, payload, abortController.signal));
  }

  async startPlanRejectStream(
    conversationId: string,
    planId: string,
    request: PlanRejectStreamRequest,
    anchorNodeId?: string | null,
  ): Promise<void> {
    const runId = `client_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    const payload: PlanRejectStreamRequest = {
      ...request,
      node_id: anchorNodeId ?? request.node_id ?? null,
    };
    const state = this.createState(
      runId,
      conversationId,
      abortController,
      null,
      null,
      'chat',
      anchorNodeId ?? request.node_id ?? null,
    );
    state.metadata = { origin: 'plan_reject', plan_id: planId };
    state.anchorUntilTargetLands = true;
    this.streams.set(runId, state);
    this.addToConversation(conversationId, runId);
    this.notify(conversationId, true);
    await this.consume(runId, () => messageApi.streamPlanReject(conversationId, planId, payload, abortController.signal));
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
    const existing = this.getConversationStates(conversationId)
      .find((state) => (runId && state.runId === runId) || (nodeId && state.targetNodeId === nodeId));
    if (existing?.status === 'streaming' || existing?.status === 'waiting_approval') return;
    if (!runId && !nodeId) return;
    const resolvedRunId = runId || `attach_${nodeId}`;
    const abortController = new AbortController();
    const state = this.createState(
      resolvedRunId,
      conversationId,
      abortController,
      null,
      nodeId,
      kind,
      anchorNodeId,
    );
    state.anchorUntilTargetLands = options.anchorUntilTargetLands ?? false;
    this.streams.set(resolvedRunId, state);
    this.addToConversation(conversationId, resolvedRunId);
    this.notify(conversationId, true);
    await this.consume(resolvedRunId, () => runId
      ? runsApi.attach(runId, fromEvent, abortController.signal)
      : messageApi.attachStream(conversationId, nodeId as string, fromEvent, abortController.signal));
  }

  private async consume(
    initialRunId: string,
    openStream: () => AsyncGenerator<any, void>,
  ): Promise<void> {
    let runId = initialRunId;
    let drained = false;
    let finishStatus: 'completed' | 'error' | 'stopped' = 'completed';
    const start = Date.now();
    const timer = window.setInterval(() => {
      const state = this.streams.get(runId);
      if (state?.status === 'streaming') {
        this.streams.set(runId, { ...state, duration: Date.now() - start });
        this.notify(state.conversationId);
      }
    }, STREAM_DURATION_UPDATE_MS);
    this.durationTimers.set(runId, timer);

    try {
      for await (const chunk of openStream()) {
        runId = this.applyChunk(runId, chunk);
        const state = this.streams.get(runId);
        const mappedStatus = mapRunStatus(chunk.status);
        if (mappedStatus === 'error') finishStatus = 'error';
        else if (mappedStatus === 'stopped') finishStatus = 'stopped';
        else if (mappedStatus === 'completed') finishStatus = 'completed';
        if (!state || state.abortController?.signal.aborted) break;
      }
      drained = true;
    } catch (err) {
      finishStatus = err instanceof Error && err.name === 'AbortError' ? 'stopped' : 'error';
      const state = this.streams.get(runId);
      if (state) {
        this.streams.set(runId, {
          ...state,
          status: finishStatus === 'error' ? 'error' : 'stopped',
          errorMessage: finishStatus === 'error' && err instanceof Error ? err.message : state.errorMessage,
          reasoningActive: false,
        });
        this.notify(state.conversationId, true);
      }
    } finally {
      clearInterval(timer);
      this.durationTimers.delete(runId);
      const state = this.streams.get(runId);
      if (state) {
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
        this.notify(finalState.conversationId, true);
        this.notifyFinish({
          conversationId: finalState.conversationId,
          runId,
          status: finalState.status === 'error' ? 'error' : finalState.status === 'stopped' ? 'stopped' : 'completed',
          drained,
          nodeId: finalState.nodeId,
          targetNodeId: finalState.targetNodeId,
          controller: finalState.abortController!,
        });
      }
    }
  }

  async stopRun(runId: string): Promise<void> {
    const state = this.streams.get(runId);
    if (!state || (state.status !== 'streaming' && state.status !== 'waiting_approval' && state.status !== 'stopping')) return;
    this.streams.set(runId, { ...state, status: 'stopping', reasoningActive: false });
    this.notify(state.conversationId, true);
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
    this.notify(conversationId);
  }

  cleanupRun(runId: string): void {
    const state = this.streams.get(runId);
    if (!state) return;
    const timer = this.durationTimers.get(runId);
    if (timer !== undefined) clearInterval(timer);
    this.durationTimers.delete(runId);
    state.abortController?.abort();
    this.streams.delete(runId);
    const set = this.runsByConversation.get(state.conversationId);
    set?.delete(runId);
    if (set && set.size === 0) this.runsByConversation.delete(state.conversationId);
    this.conversationSnapshots.delete(state.conversationId);
    this.notify(state.conversationId, true);
  }

  archiveRun(runId: string): StreamState | null {
    const resolvedRunId = this.resolveRunId(runId);
    const state = this.streams.get(resolvedRunId);
    if (!state) return null;
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
    for (const timer of this.durationTimers.values()) clearInterval(timer);
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
