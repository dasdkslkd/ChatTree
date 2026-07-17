import type { ActiveStreamInfo } from '../api/message';
import {
  connectionEpochRuntime,
  type ConnectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';

export const ACTIVE_STREAM_RECOVERY_IDLE_ATTEMPTS = 3;
export const ACTIVE_STREAM_RECOVERY_HINTED_ATTEMPTS = 10;
export const ACTIVE_STREAM_RECOVERY_FOLLOWUP_ATTEMPTS = 12;
export const ACTIVE_STREAM_RECOVERY_INTERVAL_MS = 500;

export type ActiveStreamRecoveryStatus = 'attached' | 'none' | 'paused' | 'error';

export type ActiveStreamRecoveryResult = {
  status: ActiveStreamRecoveryStatus;
  attempts: number;
  attachable: ActiveStreamInfo[];
  error?: unknown;
};

export type ActiveStreamRecoveryHandlers = {
  getActiveStreams: (conversationId: string) => Promise<ActiveStreamInfo[]>;
  isAttachable?: (stream: ActiveStreamInfo) => boolean;
  prepareAttach?: (
    conversationId: string,
    stream: ActiveStreamInfo,
    reason: string,
  ) => Promise<unknown>;
  resumeStream?: (
    conversationId: string,
    stream: ActiveStreamInfo,
    reason: string,
  ) => void;
  isPaused?: () => boolean;
  delay?: (ms: number) => Promise<void>;
};

export type ActiveStreamRecoveryProbeRequest = {
  reason?: string;
  attempts?: number;
  intervalMs?: number;
};

type Waiter = {
  resolve: (result: ActiveStreamRecoveryResult) => void;
};

type ProbeState = {
  running: boolean;
  requestedAttempts: number;
  intervalMs: number;
  reasons: Set<string>;
  waiters: Waiter[];
  epochToken: ConnectionEpochToken;
};

export type ActiveStreamRecoveryEpochSource = Pick<
  ConnectionEpochRuntime,
  'capture' | 'isCurrent' | 'signalFor'
>;

const DEFAULT_REASON = 'active-stream-recovery';

function defaultDelay(ms: number): Promise<void> {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function defaultAttachable(stream: ActiveStreamInfo): boolean {
  return !stream.done && Boolean(stream.node_id || stream.run_id);
}

function normalizeAttempts(attempts: number | undefined): number {
  if (!attempts || !Number.isFinite(attempts)) return 1;
  return Math.max(1, Math.floor(attempts));
}

function normalizeInterval(intervalMs: number | undefined): number {
  if (intervalMs == null || !Number.isFinite(intervalMs)) {
    return ACTIVE_STREAM_RECOVERY_INTERVAL_MS;
  }
  return Math.max(0, Math.floor(intervalMs));
}

export function getActiveStreamRecoveryAttemptLimit(options: {
  activeStreamHintCount: number;
}): number {
  return options.activeStreamHintCount > 0
    ? ACTIVE_STREAM_RECOVERY_HINTED_ATTEMPTS
    : ACTIVE_STREAM_RECOVERY_IDLE_ATTEMPTS;
}

export class ActiveStreamRecoveryCoordinator {
  private handlers: ActiveStreamRecoveryHandlers | null;

  private readonly states = new Map<string, ProbeState>();

  private readonly epochSource: ActiveStreamRecoveryEpochSource;

  constructor(
    handlers: ActiveStreamRecoveryHandlers | null = null,
    epochSource: ActiveStreamRecoveryEpochSource = connectionEpochRuntime,
  ) {
    this.handlers = handlers;
    this.epochSource = epochSource;
  }

  setHandlers(handlers: ActiveStreamRecoveryHandlers): void {
    this.handlers = handlers;
  }

  probeConversation(
    conversationId: string,
    request: ActiveStreamRecoveryProbeRequest = {},
  ): Promise<ActiveStreamRecoveryResult> {
    if (!this.handlers) {
      return Promise.resolve({ status: 'error', attempts: 0, attachable: [] });
    }

    let state = this.states.get(conversationId);
    if (!state) {
      let epochToken: ConnectionEpochToken;
      try {
        epochToken = this.epochSource.capture();
      } catch {
        return Promise.resolve({ status: 'paused', attempts: 0, attachable: [] });
      }
      state = {
        running: false,
        requestedAttempts: 0,
        intervalMs: ACTIVE_STREAM_RECOVERY_INTERVAL_MS,
        reasons: new Set(),
        waiters: [],
        epochToken,
      };
      this.states.set(conversationId, state);
    }

    if (!this.epochSource.isCurrent(state.epochToken)) {
      if (!state.running) {
        if (this.states.get(conversationId) === state) {
          this.states.delete(conversationId);
        }
        return Promise.resolve({ status: 'paused', attempts: 0, attachable: [] });
      }
      return new Promise<ActiveStreamRecoveryResult>((resolve) => {
        state?.waiters.push({ resolve });
      });
    }

    state.requestedAttempts = Math.max(
      state.requestedAttempts,
      normalizeAttempts(request.attempts),
    );
    state.intervalMs = Math.min(
      state.intervalMs,
      normalizeInterval(request.intervalMs),
    );
    state.reasons.add(request.reason || DEFAULT_REASON);

    const promise = new Promise<ActiveStreamRecoveryResult>((resolve) => {
      state?.waiters.push({ resolve });
    });

    if (!state.running) {
      state.running = true;
      void Promise.resolve().then(() => this.runAndResolve(conversationId, state));
    }

    return promise;
  }

  private async runAndResolve(conversationId: string, state: ProbeState): Promise<void> {
    let result: ActiveStreamRecoveryResult = {
      status: 'error',
      attempts: 0,
      attachable: [],
    };
    try {
      result = await this.runProbe(conversationId, state);
      if (!this.epochSource.isCurrent(state.epochToken)) {
        result = {
          status: 'paused',
          attempts: result.attempts,
          attachable: [],
        };
      }
    } catch (error) {
      result = this.epochSource.isCurrent(state.epochToken)
        ? { status: 'error', attempts: 0, attachable: [], error }
        : { status: 'paused', attempts: 0, attachable: [] };
    } finally {
      const waiters = state.waiters.splice(0);
      if (this.states.get(conversationId) === state) {
        this.states.delete(conversationId);
      }
      for (const waiter of waiters) waiter.resolve(result);
    }
  }

  private async runProbe(
    conversationId: string,
    state: ProbeState,
  ): Promise<ActiveStreamRecoveryResult> {
    let attempts = 0;

    while (attempts < state.requestedAttempts) {
      if (!this.epochSource.isCurrent(state.epochToken)) {
        return { status: 'paused', attempts, attachable: [] };
      }
      const handlers = this.handlers;
      if (!handlers) {
        return { status: 'error', attempts, attachable: [] };
      }
      if (handlers.isPaused?.()) {
        return { status: 'paused', attempts, attachable: [] };
      }

      let activeStreams: ActiveStreamInfo[];
      try {
        activeStreams = await handlers.getActiveStreams(conversationId);
      } catch (error) {
        if (!this.epochSource.isCurrent(state.epochToken)) {
          return { status: 'paused', attempts: attempts + 1, attachable: [] };
        }
        return { status: 'error', attempts: attempts + 1, attachable: [], error };
      }

      attempts += 1;
      if (!this.epochSource.isCurrent(state.epochToken)) {
        return { status: 'paused', attempts, attachable: [] };
      }
      const isAttachable = handlers.isAttachable ?? defaultAttachable;
      const attachable = activeStreams.filter(isAttachable);
      if (attachable.length > 0) {
        const reason = Array.from(state.reasons).join('+') || DEFAULT_REASON;
        try {
          for (const stream of attachable) {
            if (!this.epochSource.isCurrent(state.epochToken)) {
              return { status: 'paused', attempts, attachable: [] };
            }
            await handlers.prepareAttach?.(conversationId, stream, reason);
          }
        } catch (error) {
          if (!this.epochSource.isCurrent(state.epochToken)) {
            return { status: 'paused', attempts, attachable: [] };
          }
          return { status: 'error', attempts, attachable, error };
        }

        for (const stream of attachable) {
          if (!this.epochSource.isCurrent(state.epochToken)) {
            return { status: 'paused', attempts, attachable: [] };
          }
          handlers.resumeStream?.(conversationId, stream, reason);
        }
        return { status: 'attached', attempts, attachable };
      }

      if (attempts >= state.requestedAttempts) {
        return { status: 'none', attempts, attachable: [] };
      }

      await (handlers.delay ?? defaultDelay)(state.intervalMs);
      if (!this.epochSource.isCurrent(state.epochToken)) {
        return { status: 'paused', attempts, attachable: [] };
      }
    }

    return { status: 'none', attempts, attachable: [] };
  }
}
