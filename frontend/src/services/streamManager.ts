import type {
  SendMessageRequest,
} from '../types/message';
import type { TranscriptItem, TranscriptPatch } from '../types/transcript';
import { messageApi } from '../api/message';
import { runsApi } from '../api/runs';
import { ChatTreeApiError } from '../api/errors';
import { slashRegistry } from './slashRegistry';
import { flushPerfEvents } from '../perf/client';
import { perfNow, recordMark, recordSpan } from '../perf/marks';

export const STREAM_DURATION_UPDATE_MS = 1000;
const STREAM_RECONNECT_DELAYS_MS = [1000, 2000, 5000, 10000];

export interface StreamState {
  runId: string;
  status: 'idle' | 'streaming' | 'waiting_approval' | 'stopping' | 'completed' | 'error' | 'stopped';
  content: string;
  reasoning: string;
  reasoningActive: boolean;
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
  tokensUsed: number;
  duration: number;
  errorMessage: string | null;
  abortController: AbortController | null;
  pendingUserMessage: string | null;
  toolPermissionMode: string | null;
  taskContextMode: 'attached' | 'detached' | null;
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

type StatusListener = (conversationId: string) => void;

type TranscriptPatchListener = (patch: TranscriptPatch, sourceRun: Readonly<StreamState>) => void;
export type PlanActionKind = 'answer' | 'approve' | 'reject';
type TranscriptPatchStreamFactory = (signal: AbortSignal) => AsyncGenerator<TranscriptPatch, void>;

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

function mapRunStatus(status: unknown): 'streaming' | 'waiting_approval' | 'stopping' | 'completed' | 'error' | 'stopped' | null {
  if (status === 'running' || status === 'reconnecting') return 'streaming';
  if (status === 'waiting_approval') return 'waiting_approval';
  if (status === 'stopping') return 'stopping';
  if (status === 'complete' || status === 'completed') return 'completed';
  if (status === 'error' || status === 'failed') return 'error';
  if (status === 'stopped' || status === 'cancelled') return 'stopped';
  return null;
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

function normalizeTaskContextMode(value: unknown): 'attached' | 'detached' | null {
  return value === 'attached' || value === 'detached' ? value : null;
}

function waitForReconnect(delay: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) {
    return Promise.reject(new DOMException('Aborted', 'AbortError'));
  }
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(handle);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const handle = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, delay);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

function getTranscriptItemRunId(item: TranscriptItem): string | null {
  switch (item.type) {
    case 'assistant_process':
    case 'plan_question':
    case 'plan_approval':
    case 'tool_approval':
    case 'run_status':
      return item.run_id;
    default:
      return null;
  }
}

export class StreamManager {
  private streams = new Map<string, StreamState>();
  private runsByConversation = new Map<string, Set<string>>();
  private conversationSnapshots = new Map<string, { signature: string; states: StreamState[] }>();
  private listeners = new Set<StatusListener>();
  private finishListeners = new Set<FinishListener>();
  private transcriptPatchListeners = new Set<TranscriptPatchListener>();
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
      state.anchorNodeId ?? '',
      state.nodeId ?? '',
      state.targetNodeId ?? '',
      state.duration,
      state.content,
      state.reasoning,
      state.errorMessage ?? '',
      state.pendingUserMessage ?? '',
      state.toolPermissionMode ?? '',
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

  onTranscriptPatch(listener: TranscriptPatchListener): () => void {
    this.transcriptPatchListeners.add(listener);
    return () => this.transcriptPatchListeners.delete(listener);
  }

  hasRun(runId: string): boolean {
    return this.streams.has(this.resolveRunId(runId));
  }

  private emitNotify(conversationId: string) {
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

  private notify(conversationId: string, immediate = false) {
    recordMark('stream_manager.notify', {
      conversation_id: conversationId,
      immediate,
      pending: this.pendingNotifyHandles.has(conversationId),
    });
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

  private notifyTranscriptPatch(patch: TranscriptPatch, sourceRun: Readonly<StreamState>) {
    this.transcriptPatchListeners.forEach((listener) => listener(patch, sourceRun));
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

  private applyTranscriptPatchChunk(runId: string, patch: TranscriptPatch): string {
    let state = this.streams.get(runId);
    if (!state) return runId;
    const patchRunId = patch.operations
      .map((operation) => operation.op === 'upsert' ? getTranscriptItemRunId(operation.item) : null)
      .find((value): value is string => typeof value === 'string' && value.length > 0);
    if (patchRunId && patchRunId !== runId) {
      runId = this.replaceRunId(runId, patchRunId);
      state = this.streams.get(runId);
      if (!state) return runId;
    }

    const renderedItems = patch.operations
      .filter((operation) => operation.op === 'upsert')
      .map((operation) => operation.item);
    const nodeId = patch.node_id || state.nodeId;
    const statusItem = renderedItems.find((item) => item.type === 'run_status')
      ?? renderedItems.find((item) => item.type === 'assistant_process');
    const mappedStatus = mapRunStatus(statusItem?.status);
    const answerItem = renderedItems.find((item) => item.type === 'assistant_answer');
    const answerFailureStatus = answerItem?.status === 'error' || answerItem?.status === 'stopped'
      ? mapRunStatus(answerItem.status)
      : null;
    const nextStatus = mappedStatus ?? answerFailureStatus ?? state.status;
    const nextErrorMessage = nextStatus === 'error' && statusItem && 'message' in statusItem
      ? statusItem.message?.trim() || state.errorMessage
      : state.errorMessage;
    const userMessageItem = renderedItems.find((item) => item.type === 'user_message');
    const nextToolPermissionMode = (userMessageItem && 'tool_permission_mode' in userMessageItem)
      ? (userMessageItem as { tool_permission_mode?: string | null }).tool_permission_mode ?? state.toolPermissionMode
      : state.toolPermissionMode;

    const nextState = {
      ...state,
      status: nextStatus,
      content: typeof answerItem?.content === 'string' ? answerItem.content : state.content,
      nodeId,
      targetNodeId: nodeId,
      conversationId: patch.conversation_id || state.conversationId,
      reasoningActive: false,
      errorMessage: nextErrorMessage,
      toolPermissionMode: nextToolPermissionMode,
    };
    this.streams.set(runId, nextState);
    this.notifyTranscriptPatch(patch, nextState);
    this.notify(patch.conversation_id || state.conversationId, true);
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
      tokensUsed: 0,
      duration: 0,
      errorMessage: null,
      abortController: controller,
      pendingUserMessage,
      toolPermissionMode: null,
      taskContextMode: null,
      createdAt: Date.now(),
    };
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
    const idempotencyKey = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `message-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    try {
      const started = await messageApi.startRun(
        conversationId,
        payload,
        idempotencyKey,
        abortController.signal,
      );
      runId = this.replaceRunId(runId, started.run_id);
      await this.consume(runId, () => runsApi.attach(runId, { signal: abortController.signal }));
    } catch (err) {
      const status = err instanceof Error && err.name === 'AbortError' ? 'stopped' : 'error';
      const state = this.streams.get(runId);
      if (!state) return;
      const finalState = {
        ...state,
        status,
        errorMessage: status === 'error' && err instanceof Error ? err.message : state.errorMessage,
        reasoningActive: false,
      } as StreamState;
      this.streams.set(runId, finalState);
      this.notify(finalState.conversationId, true);
      this.notifyFinish({
        conversationId: finalState.conversationId,
        runId,
        status,
        drained: false,
        nodeId: finalState.nodeId,
        targetNodeId: finalState.targetNodeId,
        controller: abortController,
      });
    }
  }

  async resumeStream(
    conversationId: string,
    nodeId: string | null,
    runId?: string,
    anchorNodeId?: string | null,
    kind = 'chat',
  ): Promise<void> {
    const existing = this.getConversationStates(conversationId)
      .find((state) => (runId && state.runId === runId) || (nodeId && state.targetNodeId === nodeId));
    if (existing?.status === 'streaming' || existing?.status === 'stopping') return;
    if (
      existing?.status === 'waiting_approval'
      && existing.abortController
      && !existing.abortController.signal.aborted
    ) return;
    if (!runId && !nodeId) return;
    if (runId) {
      const run = await runsApi.get(runId);
      if (!['queued', 'running', 'waiting_approval', 'stopping'].includes(run.status)) return;
    }
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
    this.streams.set(resolvedRunId, state);
    this.addToConversation(conversationId, resolvedRunId);
    this.notify(conversationId, true);
    await this.consume(resolvedRunId, () => runId
      ? runsApi.attach(runId, { signal: abortController.signal })
      : messageApi.attachStream(
          conversationId,
          nodeId as string,
          { signal: abortController.signal },
        ));
  }

  async startPlanActionStream(
    conversationId: string,
    nodeId: string,
    action: PlanActionKind,
    openStream: TranscriptPatchStreamFactory,
  ): Promise<void> {
    const runId = `client_plan_${action}_${Date.now()}_${this.tempSeq++}`;
    const abortController = new AbortController();
    const state = this.createState(
      runId,
      conversationId,
      abortController,
      null,
      nodeId,
      'chat',
      nodeId,
    );
    state.metadata = { plan_action: action };
    this.streams.set(runId, state);
    this.addToConversation(conversationId, runId);
    this.notify(conversationId, true);
    await this.consumeTranscriptPatchOnly(runId, () => openStream(abortController.signal));
  }

  private async consume(
    initialRunId: string,
    openStream: () => AsyncGenerator<TranscriptPatch, void>,
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
      let streamFactory = openStream;
      let reconnectAttempt = 0;
      while (true) {
        try {
          for await (const chunk of streamFactory()) {
            const applyStarted = perfNow();
            if (chunk?.type !== 'transcript_patch') {
              throw new Error('Unexpected stream event: expected transcript_patch');
            }
            runId = this.applyTranscriptPatchChunk(runId, chunk);
            recordSpan('stream_manager.apply_chunk', applyStarted, {
              run_id: runId,
              revision: chunk.revision,
              operation_count: chunk.operations.length,
            });
            const state = this.streams.get(runId);
            const mappedStatus = state?.status ?? null;
            if (mappedStatus === 'error') finishStatus = 'error';
            else if (mappedStatus === 'stopped') finishStatus = 'stopped';
            else if (mappedStatus === 'completed') finishStatus = 'completed';
            if (!state || state.abortController?.signal.aborted) break;
          }
          drained = true;
          break;
        } catch (err) {
          const state = this.streams.get(runId);
          const signal = state?.abortController?.signal;
          if (
            !state
            || signal?.aborted
            || runId.startsWith('client_')
            || runId.startsWith('attach_')
            || (err instanceof ChatTreeApiError && !err.retryable)
          ) {
            throw err;
          }
          try {
            await runsApi.get(runId);
          } catch (statusError) {
            if (statusError instanceof ChatTreeApiError && !statusError.retryable) {
              throw err;
            }
          }
          const delay = STREAM_RECONNECT_DELAYS_MS[
            Math.min(reconnectAttempt, STREAM_RECONNECT_DELAYS_MS.length - 1)
          ];
          reconnectAttempt += 1;
          await waitForReconnect(delay, signal!);
          streamFactory = () => runsApi.attach(runId, { signal });
        }
      }
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
        void flushPerfEvents();
      }
    }
  }

  private async consumeTranscriptPatchOnly(
    initialRunId: string,
    openStream: () => AsyncGenerator<TranscriptPatch, void>,
  ): Promise<void> {
    let runId = initialRunId;
    let finishStatus: 'completed' | 'error' | 'stopped' = 'completed';
    let caughtError: unknown = null;
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
      for await (const patch of openStream()) {
        if (patch?.type !== 'transcript_patch') {
          throw new Error('Unexpected plan action stream event: expected transcript_patch');
        }
        const applyStarted = perfNow();
        runId = this.applyTranscriptPatchChunk(runId, patch);
        recordSpan('stream_manager.apply_plan_action_patch', applyStarted, {
          run_id: runId,
          revision: patch.revision,
          operation_count: patch.operations.length,
        });
        const state = this.streams.get(runId);
        if (!state || state.abortController?.signal.aborted) break;
      }
    } catch (err) {
      caughtError = err;
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
        this.streams.set(runId, {
          ...state,
          status: finalStatus,
          duration: Date.now() - start,
          reasoningActive: false,
        });
        this.notify(state.conversationId, true);
        if (!caughtError && finalStatus !== 'error') {
          this.cleanupRun(runId);
        }
        void flushPerfEvents();
      }
    }
    if (caughtError) throw caughtError;
  }

  async stopRun(runId: string): Promise<void> {
    const state = this.streams.get(runId);
    if (!state || (state.status !== 'streaming' && state.status !== 'waiting_approval' && state.status !== 'stopping')) return;
    this.streams.set(runId, { ...state, status: 'stopping', reasoningActive: false });
    this.notify(state.conversationId, true);
    const stopRequest = !runId.startsWith('client_') && !runId.startsWith('attach_')
      ? runsApi.stop(runId)
      : Promise.resolve();
    state.abortController?.abort();
    try {
      await stopRequest;
    } catch {
      state.abortController?.abort();
    }
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
    this.transcriptPatchListeners.clear();
    this.durationTimers.clear();
    this.pendingNotifyHandles.clear();
  }
}

export const streamManager = new StreamManager();
