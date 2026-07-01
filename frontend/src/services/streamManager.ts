import type { SendMessageRequest, ToolApprovalPayload } from '../types/message';
import { messageApi } from '../api/message';
import { runsApi } from '../api/runs';
import { slashRegistry } from './slashRegistry';

export const STREAM_DURATION_UPDATE_MS = 1000;

export interface StreamState {
  runId: string;
  status: 'idle' | 'streaming' | 'completed' | 'error' | 'stopped';
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
  tokensUsed: number;
  duration: number;
  errorMessage: string | null;
  abortController: AbortController | null;
  pendingUserMessage: string | null;
  eventCount: number;
  createdAt: number;
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

function mapRunStatus(status: unknown): 'completed' | 'error' | 'stopped' | null {
  if (status === 'complete' || status === 'completed') return 'completed';
  if (status === 'error' || status === 'failed') return 'error';
  if (status === 'stopped' || status === 'cancelled') return 'stopped';
  return null;
}

function getRequestRunKind(request: SendMessageRequest): string {
  const match = slashRegistry.match(request.content);
  return match?.command.run_kind || 'chat';
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
      .filter((state) => state.status === 'streaming' || state.status === 'stopped' || state.status === 'error');
    const streaming = states
      .filter((state) => state.status === 'streaming')
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
    ].join(':')).join('|');
    const cached = this.conversationSnapshots.get(conversationId);
    if (cached?.signature === signature) return cached.states;
    this.conversationSnapshots.set(conversationId, { signature, states });
    return states;
  }

  isStreaming(conversationId?: string): boolean {
    const states = conversationId ? this.getConversationStates(conversationId) : [...this.streams.values()];
    return states.some((state) => state.status === 'streaming');
  }

  getStreamingConversationIds(): string[] {
    const ids: string[] = [];
    for (const [conversationId, runIds] of this.runsByConversation.entries()) {
      if ([...runIds].some((runId) => this.streams.get(runId)?.status === 'streaming')) {
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
      return this.streams.get(resolved)?.status !== 'streaming';
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
    if (chunk.anchor_node_id !== undefined && !next.targetNodeId) {
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
    if (chunk.content) {
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
      tokensUsed: 0,
      duration: 0,
      errorMessage: null,
      abortController: controller,
      pendingUserMessage,
      eventCount: 0,
      createdAt: Date.now(),
    };
  }

  async startStream(
    conversationId: string,
    request: SendMessageRequest,
    pendingUserMessage: string | null = null,
    nodeId?: string,
  ): Promise<void> {
    let runId = `client_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    this.streams.set(runId, this.createState(
      runId,
      conversationId,
      abortController,
      pendingUserMessage,
      nodeId ?? null,
      getRequestRunKind(request),
    ));
    this.addToConversation(conversationId, runId);
    this.notify(conversationId, true);
    await this.consume(runId, () => messageApi.stream(conversationId, request, nodeId, abortController.signal));
  }

  async resumeStream(
    conversationId: string,
    nodeId: string | null,
    runId?: string,
    fromEvent = 0,
    anchorNodeId?: string | null,
  ): Promise<void> {
    const existing = this.getConversationStates(conversationId)
      .find((state) => (runId && state.runId === runId) || (nodeId && state.targetNodeId === nodeId));
    if (existing?.status === 'streaming') return;
    if (!runId && !nodeId) return;
    const resolvedRunId = runId || `attach_${nodeId}`;
    const abortController = new AbortController();
    this.streams.set(resolvedRunId, this.createState(
      resolvedRunId,
      conversationId,
      abortController,
      null,
      nodeId,
      'chat',
      anchorNodeId,
    ));
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
        const finalState = {
          ...state,
          status: state.status === 'streaming' ? finishStatus : state.status,
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
    if (!state || state.status !== 'streaming') return;
    this.streams.set(runId, { ...state, status: 'stopped', reasoningActive: false });
    this.notify(state.conversationId, true);
    try {
      if (runId.startsWith('run_')) await runsApi.stop(runId);
      else if (state.targetNodeId) await messageApi.stopStream(state.conversationId, state.targetNodeId);
      else state.abortController?.abort();
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
