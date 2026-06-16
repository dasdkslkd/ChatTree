import type { SendMessageRequest } from '../types/message';
import { messageApi } from '../api/message';

interface StreamState {
  status: 'idle' | 'streaming' | 'completed' | 'error' | 'stopped';
  content: string;
  nodeId: string | null;
  tokensUsed: number;
  duration: number;
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

export class StreamManager {
  private streams = new Map<string, StreamState>();
  private listeners = new Set<StatusListener>();
  private finishListeners = new Set<FinishListener>();
  private abortControllers = new Map<string, AbortController>();
  private durationTimers = new Map<string, number>();

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
    }

    const abortController = new AbortController();
    this.abortControllers.set(conversationId, abortController);

    // Initialize stream state
    let state: StreamState = {
      status: 'streaming',
      content: '',
      nodeId: null,
      tokensUsed: 0,
      duration: 0,
      abortController,
      pendingUserMessage,
    };
    this.streams.set(conversationId, state);
    this.notify(conversationId);

    let fullContent = '';
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
          fullContent += chunk.content;
          state = { ...state, content: fullContent };
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
          state = { ...state, duration: Date.now() - startTime, status: 'completed' as const };
        } else if (chunk.status === 'stopped') {
          finishStatus = 'stopped';
          state = { ...state, duration: Date.now() - startTime, status: 'stopped' as const };
        } else if (chunk.status === 'error') {
          finishStatus = 'error';
          state = { ...state, duration: Date.now() - startTime, status: 'error' as const };
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
          duration: Date.now() - startTime,
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
  }
}

export const streamManager = new StreamManager();