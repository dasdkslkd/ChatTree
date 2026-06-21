import type { SendMessageRequest, ToolApprovalPayload } from '../types/message';
import { messageApi } from '../api/message';

interface StreamState {
  status: 'idle' | 'streaming' | 'completed' | 'error' | 'stopped';
  content: string;
  reasoning: string;  // 累积的思考过程增量（event_type==="reasoning"）
  reasoningActive: boolean;
  toolInteractions: any[];
  pendingApprovals: Record<string, ToolApprovalPayload>;
  nodeId: string | null;
  tokensUsed: number;
  duration: number;
  errorMessage: string | null;
  abortController: AbortController | null;
  // 乐观渲染的用户消息（按对话隔离，并发时互不干扰）
  pendingUserMessage: string | null;
}

type StatusListener = (conversationId: string) => void;
// 流终止时触发（completed/error/stopped 都会触发），用于刷新视图。
// 不依赖任何 React 订阅者是否挂载，因此切走的对话也能正确落地。
// drained=true 表示已读到 [DONE]/连接正常关闭（后端保存已完成，刷新安全）；
// drained=false 表示是硬 abort（连接被切断），后端保存与刷新存在竞态。
// controller 用于让监听者做“身份校验清理”，避免清掉已被新流取代的状态。
interface FinishInfo {
  conversationId: string;
  status: 'completed' | 'error' | 'stopped';
  drained: boolean;
  nodeId: string | null;
  controller: AbortController;
}
type FinishListener = (info: FinishInfo) => void;

interface DisplayPump {
  contentTarget: string;
  reasoningTarget: string;
  contentShown: string;
  reasoningShown: string;
  timer: number | null;
  controller: AbortController;
}

function mergeToolCalls(existing: any[], incoming: any[]): any[] {
  const merged = incoming.length > 0
    ? existing.filter((toolCall) => !toolCall?.pending)
    : [...existing];
  for (const toolCall of incoming) {
    const key = toolCall?.id ?? toolCall?.index;
    const idx = key == null ? -1 : merged.findIndex((item) => (item?.id ?? item?.index) === key);
    if (idx >= 0) {
      merged[idx] = { ...merged[idx], ...toolCall };
    } else {
      merged.push(toolCall);
    }
  }
  return merged;
}

function getChunkToolCalls(chunk: any): any[] {
  if (Array.isArray(chunk.tool_calls)) return chunk.tool_calls;
  if (Array.isArray(chunk.tool_call?.tool_calls)) return chunk.tool_call.tool_calls;
  if (chunk.tool_call && typeof chunk.tool_call === 'object') return [chunk.tool_call];
  return [];
}

function createPendingToolCall() {
  return {
    id: '__pending_tool_call__',
    type: 'function',
    pending: true,
    function: {
      name: '准备工具调用',
      arguments: '',
    },
  };
}

function appendToolCallStart(toolInteractions: any[], content: string, reasoning: string): any[] {
  const next = [...toolInteractions];
  const last = next[next.length - 1];
  if (
    last &&
    Array.isArray(last?.assistant?.tool_calls) &&
    last.assistant.tool_calls.some((toolCall: any) => toolCall?.pending) &&
    (!Array.isArray(last.tools) || last.tools.length === 0)
  ) {
    next[next.length - 1] = {
      ...last,
      assistant: {
        ...last.assistant,
        content: content || last.assistant?.content || '',
      },
      reasoning: reasoning || last.reasoning || null,
    };
    return next;
  }
  next.push({
    assistant: {
      role: 'assistant',
      content,
      tool_calls: [createPendingToolCall()],
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
    assistant: {
      role: 'assistant',
      content,
      tool_calls: toolCalls,
    },
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
    : [{
        assistant: { role: 'assistant', content: '', tool_calls: [] },
        tools: [],
        reasoning: null,
      }];

  const targetId = toolResult.tool_call_id;
  const targetIndex = targetId
    ? next.findIndex((interaction) => (interaction.assistant?.tool_calls || []).some((call: any) => call?.id === targetId))
    : -1;
  const index = targetIndex >= 0 ? targetIndex : next.length - 1;
  const tools = next[index].tools;
  const existingIndex = targetId ? tools.findIndex((tool: any) => tool?.tool_call_id === targetId) : -1;
  if (existingIndex >= 0) {
    tools[existingIndex] = { ...tools[existingIndex], ...toolResult };
  } else {
    tools.push({
      role: 'tool',
      ...toolResult,
    });
  }
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

export class StreamManager {
  private streams = new Map<string, StreamState>();
  private listeners = new Set<StatusListener>();
  private finishListeners = new Set<FinishListener>();
  private abortControllers = new Map<string, AbortController>();
  private durationTimers = new Map<string, number>();
  private displayPumps = new Map<string, DisplayPump>();
  private immediateDisplayConversations = new Set<string>();

  /** Get state for a given conversation */
  getState(conversationId: string): Readonly<StreamState> | undefined {
    return this.streams.get(conversationId);
  }

  /** Check if any conversation is streaming */
  isStreaming(conversationId?: string): boolean {
    if (conversationId) {
      const s = this.streams.get(conversationId);
      return s?.status === 'streaming';
    }
    for (const s of this.streams.values()) {
      if (s.status === 'streaming') return true;
    }
    return false;
  }

  /** Subscribe to stream state changes */
  subscribe(listener: StatusListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Subscribe to stream finish events (completed/error/stopped) */
  onFinish(listener: FinishListener): () => void {
    this.finishListeners.add(listener);
    return () => this.finishListeners.delete(listener);
  }

  private notify(conversationId: string) {
    this.listeners.forEach((l) => l(conversationId));
  }

  private notifyFinish(info: FinishInfo) {
    this.finishListeners.forEach((l) => l(info));
  }

  private getDisplayBatchSize(remaining: number): number {
    if (remaining > 4000) return 240;
    if (remaining > 2000) return 120;
    if (remaining > 1000) return 64;
    if (remaining > 480) return 32;
    if (remaining > 160) return 16;
    if (remaining > 60) return 8;
    if (remaining > 20) return 3;
    return 1;
  }

  private getDisplayDelay(remaining: number): number {
    if (remaining > 480) return 0;
    if (remaining > 160) return 8;
    if (remaining > 60) return 14;
    if (remaining > 20) return 22;
    return 34;
  }

  private advanceText(current: string, target: string): string {
    if (!target.startsWith(current)) return target;
    const remaining = target.length - current.length;
    if (remaining <= 0) return current;
    const nextPart = Array.from(target.slice(current.length))
      .slice(0, this.getDisplayBatchSize(remaining))
      .join('');
    return target.slice(0, current.length) + nextPart;
  }

  private clearDisplayPump(conversationId: string): void {
    const pump = this.displayPumps.get(conversationId);
    if (pump?.timer != null) {
      clearTimeout(pump.timer);
    }
    this.displayPumps.delete(conversationId);
  }

  private ensureDisplayPump(conversationId: string, controller: AbortController): DisplayPump {
    let pump = this.displayPumps.get(conversationId);
    const state = this.streams.get(conversationId);
    if (!pump || pump.controller !== controller) {
      if (pump?.timer != null) clearTimeout(pump.timer);
      pump = {
        contentTarget: state?.content ?? '',
        reasoningTarget: state?.reasoning ?? '',
        contentShown: state?.content ?? '',
        reasoningShown: state?.reasoning ?? '',
        timer: null,
        controller,
      };
      this.displayPumps.set(conversationId, pump);
    }
    return pump;
  }

  private setDisplayTargets(
    conversationId: string,
    controller: AbortController,
    targets: Partial<Pick<DisplayPump, 'contentTarget' | 'reasoningTarget'>>,
  ): void {
    const pump = this.ensureDisplayPump(conversationId, controller);
    if (targets.contentTarget !== undefined) pump.contentTarget = targets.contentTarget;
    if (targets.reasoningTarget !== undefined) pump.reasoningTarget = targets.reasoningTarget;
    if (this.immediateDisplayConversations.has(conversationId)) {
      this.flushDisplayPump(conversationId, controller, true);
      return;
    }
    if (pump.timer == null) {
      pump.timer = window.setTimeout(() => {
        this.flushDisplayPump(conversationId, controller, false);
      }, 0);
    }
  }

  private flushDisplayPump(conversationId: string, controller: AbortController, force: boolean): void {
    const pump = this.displayPumps.get(conversationId);
    if (!pump || pump.controller !== controller) return;
    pump.timer = null;

    const current = this.streams.get(conversationId);
    if (!current || this.abortControllers.get(conversationId) !== controller) {
      this.clearDisplayPump(conversationId);
      return;
    }

    const nextContent = force ? pump.contentTarget : this.advanceText(pump.contentShown, pump.contentTarget);
    const nextReasoning = force ? pump.reasoningTarget : this.advanceText(pump.reasoningShown, pump.reasoningTarget);
    const changed = nextContent !== pump.contentShown || nextReasoning !== pump.reasoningShown;

    pump.contentShown = nextContent;
    pump.reasoningShown = nextReasoning;

    if (changed || current.content !== nextContent || current.reasoning !== nextReasoning) {
      this.streams.set(conversationId, {
        ...current,
        content: nextContent,
        reasoning: nextReasoning,
      });
      this.notify(conversationId);
    }

    const remaining = Math.max(
      pump.contentTarget.length - pump.contentShown.length,
      pump.reasoningTarget.length - pump.reasoningShown.length,
    );

    if (!force && remaining > 0) {
      pump.timer = window.setTimeout(() => {
        this.flushDisplayPump(conversationId, controller, false);
      }, this.getDisplayDelay(remaining));
    } else if (force) {
      this.clearDisplayPump(conversationId);
    }
  }

  private waitForDisplayDrain(
    conversationId: string,
    controller: AbortController,
    maxWaitMs = 10000,
  ): Promise<void> {
    const startedAt = Date.now();
    return new Promise((resolve) => {
      const check = () => {
        const pump = this.displayPumps.get(conversationId);
        const replaced = this.abortControllers.get(conversationId) !== controller;
        const drained =
          !pump ||
          (pump.contentShown === pump.contentTarget && pump.reasoningShown === pump.reasoningTarget);
        if (replaced || drained || Date.now() - startedAt >= maxWaitMs) {
          resolve();
          return;
        }
        const remaining = pump
          ? Math.max(
              pump.contentTarget.length - pump.contentShown.length,
              pump.reasoningTarget.length - pump.reasoningShown.length,
            )
          : 0;
        window.setTimeout(check, this.getDisplayDelay(remaining));
      };
      check();
    });
  }

  private flushDisplayImmediately(conversationId: string): void {
    this.immediateDisplayConversations.add(conversationId);
    const controller = this.abortControllers.get(conversationId);
    if (controller) {
      this.flushDisplayPump(conversationId, controller, true);
    }
  }

  /** Start a new stream for a conversation */
  async startStream(
    conversationId: string,
    request: SendMessageRequest,
    pendingUserMessage: string | null = null,
    nodeId?: string,
  ): Promise<void> {
    // Abort any existing stream for this conversation
    const existingController = this.abortControllers.get(conversationId);
    if (existingController) {
      existingController.abort();
      this.clearDisplayPump(conversationId);
    }
    this.immediateDisplayConversations.delete(conversationId);

    const abortController = new AbortController();
    this.abortControllers.set(conversationId, abortController);

    // Initialize stream state
    let state: StreamState = {
      status: 'streaming',
      content: '',
      reasoning: '',
      reasoningActive: false,
      toolInteractions: [],
      pendingApprovals: {},
      nodeId: null,
      tokensUsed: 0,
      duration: 0,
      errorMessage: null,
      abortController,
      pendingUserMessage,
    };
    this.streams.set(conversationId, state);
    this.notify(conversationId);

    let currentContent = '';
    let currentReasoning = '';
    const startTime = Date.now();
    // 终止状态：默认 completed，被显式 stop / error 时改写。
    let finishStatus: 'completed' | 'error' | 'stopped' = 'completed';
    // 是否把流读到了 [DONE]/连接正常关闭。true → 后端保存已完成，刷新安全；
    // false → 走了 catch（abort/网络错误）或提前 break，与后端保存存在竞态。
    let drained = false;
    // 是否因 abort/supersede/state 丢失而提前退出循环（非正常 drain）。
    let brokeEarly = false;

    // Start duration timer — 只更新 duration 字段，合并到 map 中的最新值，
    // 避免主循环用过期的局部 state 覆盖。
    if (this.durationTimers.has(conversationId)) {
      clearInterval(this.durationTimers.get(conversationId)!);
    }
    // 把定时器 id 存到局部变量，finally 里只清理“自己的”定时器，
    // 避免被新流取代后误删新流的定时器。
    const myTimer = window.setInterval(() => {
      const s = this.streams.get(conversationId);
      if (s && s.status === 'streaming') {
        this.streams.set(conversationId, { ...s, duration: Date.now() - startTime });
        this.notify(conversationId);
      }
    }, 100);
    this.durationTimers.set(conversationId, myTimer);

    try {
      for await (const chunk of messageApi.stream(conversationId, request, nodeId, abortController.signal)) {
        // 被新的流取代（同一对话再次发起）→ 让旧流退出（不算正常 drain）
        if (this.abortControllers.get(conversationId) !== abortController) {
          brokeEarly = true;
          break;
        }
        // 被 abort（硬中断）→ 连接已切断，后端保存由断连触发，与刷新竞态。
        // 不算正常 drain，避免被当成“安全路径”而跳过重试。
        if (abortController.signal.aborted) {
          brokeEarly = true;
          break;
        }

        // 始终合并到 map 中的最新 state（duration 由计时器写入），再覆盖回去。
        const current = this.streams.get(conversationId);
        if (!current) { brokeEarly = true; break; }
        state = current;

        if (chunk.content) {
          if (currentReasoning && state.reasoning !== currentReasoning) {
            this.flushDisplayPump(conversationId, abortController, true);
            const flushedState = this.streams.get(conversationId);
            if (!flushedState) { brokeEarly = true; break; }
            state = flushedState;
          }
          currentContent += chunk.content;
          this.setDisplayTargets(conversationId, abortController, { contentTarget: currentContent });
          state = { ...state, reasoningActive: false };
        }
        if (chunk.reasoning) {
          currentReasoning += chunk.reasoning;
          this.setDisplayTargets(conversationId, abortController, { reasoningTarget: currentReasoning });
          state = { ...state, reasoningActive: true };
        }
        const displayedState = this.streams.get(conversationId);
        if (displayedState) {
          state = { ...state, content: displayedState.content, reasoning: displayedState.reasoning };
        }
        if (chunk.event_type === 'tool_call_start') {
          this.flushDisplayPump(conversationId, abortController, true);
          const flushedState = this.streams.get(conversationId);
          if (!flushedState) { brokeEarly = true; break; }
          state = {
            ...flushedState,
            content: '',
            reasoning: '',
            reasoningActive: false,
            toolInteractions: appendToolCallStart(flushedState.toolInteractions, currentContent, currentReasoning),
          };
          currentContent = '';
          currentReasoning = '';
          this.clearDisplayPump(conversationId);
        } else if (chunk.event_type === 'tool_call') {
          const toolCalls = getChunkToolCalls(chunk);
          if (toolCalls.length > 0) {
            this.flushDisplayPump(conversationId, abortController, true);
            const flushedState = this.streams.get(conversationId);
            if (!flushedState) { brokeEarly = true; break; }
            state = flushedState;
          }
          state = {
            ...state,
            toolInteractions: appendToolCalls(state.toolInteractions, toolCalls, currentContent, currentReasoning),
          };
          if (toolCalls.length > 0) {
            currentContent = '';
            currentReasoning = '';
            this.clearDisplayPump(conversationId);
            state = { ...state, content: '', reasoning: '', reasoningActive: false };
          }
        } else if (chunk.event_type === 'tool_result') {
          state = {
            ...state,
            toolInteractions: appendToolResult(state.toolInteractions, chunk.tool_call),
            reasoningActive: false,
          };
        }
        if (chunk.event_type === 'tool_approval_request') {
          state = {
            ...state,
            pendingApprovals: mergeApproval(state.pendingApprovals, chunk.approval, 'pending'),
          };
        } else if (chunk.event_type === 'tool_approval_result') {
          state = {
            ...state,
            pendingApprovals: mergeApproval(state.pendingApprovals, chunk.approval),
          };
        }
        if (chunk.node_id) {
          state = { ...state, nodeId: chunk.node_id };
        }
        if (chunk.tokens_used) {
          state = { ...state, tokensUsed: chunk.tokens_used };
        }

        // 终止状态：记录并更新 UI，但**不**提前 break/throw。
        // 必须把流读到 [DONE]（或连接关闭），因为后端在发送 [DONE] 之前
        // 才会把助手消息保存到磁盘。提前断开会与保存产生竞态，导致
        // refreshMessages 拉到的是旧数据。
        if (chunk.status === 'complete') {
          finishStatus = 'completed';
          state = { ...state, duration: Date.now() - startTime, status: 'completed' as const, reasoningActive: false };
        } else if (chunk.status === 'stopped') {
          finishStatus = 'stopped';
          state = { ...state, duration: Date.now() - startTime, status: 'stopped' as const, reasoningActive: false };
        } else if (chunk.status === 'error') {
          finishStatus = 'error';
          const chunkError = typeof chunk.error === 'string' && chunk.error.trim()
            ? chunk.error
            : state.errorMessage;
          state = {
            ...state,
            duration: Date.now() - startTime,
            status: 'error' as const,
            errorMessage: chunkError,
            reasoningActive: false,
          };
        }

        this.streams.set(conversationId, state);
        this.notify(conversationId);
      }
      // for-await 正常结束且未提前 break：读到 [DONE]/连接正常关闭，后端保存已完成。
      drained = !brokeEarly;
    } catch (err) {
      // 网络层异常 / abort：后端的 finally 仍会保存已生成的部分内容。
      if (err instanceof Error && err.name === 'AbortError') {
        finishStatus = 'stopped';
      } else {
        finishStatus = 'error';
      }
      const errorMessage = err instanceof Error ? err.message : String(err);
      // 仅当本流仍是活跃流时才写回状态。被新流取代时，map 里已是新流的
      // state，不能覆盖（否则把进行中的新流标记为 stopped/error）。
      const finalState = this.streams.get(conversationId);
      if (
        this.abortControllers.get(conversationId) === abortController &&
        finalState &&
        finalState.status === 'streaming'
      ) {
        this.streams.set(conversationId, {
          ...finalState, status: finishStatus === 'error' ? 'error' : 'stopped',
          errorMessage: finishStatus === 'error' ? errorMessage : finalState.errorMessage,
          duration: Date.now() - startTime,
          reasoningActive: false,
        });
        this.notify(conversationId);
      }
    } finally {
      // 只清理自己创建的定时器；若已被新流替换则保留新流的定时器。
      clearInterval(myTimer);
      if (this.durationTimers.get(conversationId) === myTimer) {
        this.durationTimers.delete(conversationId);
      }
      // 仅当本流仍是该对话的活跃流（未被新流取代、未被 cleanup）时才通知结束。
      if (this.abortControllers.get(conversationId) === abortController) {
        await this.waitForDisplayDrain(conversationId, abortController);
        if (this.abortControllers.get(conversationId) !== abortController) {
          return;
        }
        const finalState = this.streams.get(conversationId);
        this.notifyFinish({
          conversationId,
          status: finishStatus,
          drained,
          nodeId: finalState?.nodeId ?? null,
          controller: abortController,
        });
      }
    }
  }

  /** Stop an active stream (graceful) */
  async stopStream(conversationId: string): Promise<void> {
    const state = this.streams.get(conversationId);
    if (!state || state.status !== 'streaming') return;
    this.flushDisplayImmediately(conversationId);
    const displayedState = this.streams.get(conversationId) ?? state;
    this.streams.set(conversationId, { ...displayedState, status: 'stopped', reasoningActive: false });
    this.notify(conversationId);

    // 请求后端停止生成。后端会发出 STOPPED chunk → 保存对话 → 发送 [DONE]，
    // 客户端的 startStream 循环会把这些读完后在 finally 里触发 notifyFinish。
    // 因此**不要**在这里 abort fetch —— 那会让客户端提前断连，与后端保存竞态。
    if (state.nodeId) {
      try {
        await messageApi.stopStream(conversationId, state.nodeId);
      } catch (_) {
        // 后端报错则退回到硬中断，至少释放连接
        const controller = this.abortControllers.get(conversationId);
        controller?.abort();
      }
    } else {
      // 还没拿到 nodeId（停得太早）：只能硬中断
      const controller = this.abortControllers.get(conversationId);
      controller?.abort();
    }
  }

  /** Clean up state for a conversation */
  cleanup(conversationId: string): void {
    this.clearDisplayPump(conversationId);
    this.immediateDisplayConversations.delete(conversationId);
    const timerId = this.durationTimers.get(conversationId);
    if (timerId !== undefined) {
      clearInterval(timerId);
      this.durationTimers.delete(conversationId);
    }
    const controller = this.abortControllers.get(conversationId);
    controller?.abort();
    this.abortControllers.delete(conversationId);
    this.streams.delete(conversationId);
    // 通知订阅者：该对话的流状态已移除（乐观气泡/流式内容消失）。
    // onFinish 中先 await refreshMessages 再 cleanup，React 19 自动批处理
    // 使“注入真实消息 + 移除临时状态”在同一帧提交，无闪烁、无重复。
    this.notify(conversationId);
  }

  /**
   * 身份校验清理：仅当该对话的活跃 controller 仍是传入的 controller 时才清理。
   * 用于 onFinish 的异步收尾——若在 await 期间用户对同一对话发起了新流
   * （controller 已被替换），则不应清掉新流的状态。
   */
  cleanupIfController(conversationId: string, controller: AbortController): void {
    if (this.abortControllers.get(conversationId) === controller) {
      this.cleanup(conversationId);
    }
  }

  /** Reset all streams */
  resetAll(): void {
    for (const [convId, controller] of this.abortControllers) {
      controller.abort();
      const timerId = this.durationTimers.get(convId);
      if (timerId !== undefined) {
        clearInterval(timerId);
      }
    }
    this.streams.clear();
    this.listeners.clear();
    this.finishListeners.clear();
    this.abortControllers.clear();
    this.durationTimers.clear();
    this.immediateDisplayConversations.clear();
    for (const pump of this.displayPumps.values()) {
      if (pump.timer != null) clearTimeout(pump.timer);
    }
    this.displayPumps.clear();
  }
}

export const streamManager = new StreamManager();
