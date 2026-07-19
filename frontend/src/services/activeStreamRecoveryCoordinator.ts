import type { ActiveStreamInfo } from '../api/message';

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
};

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

  constructor(handlers: ActiveStreamRecoveryHandlers | null = null) {
    this.handlers = handlers;
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
      state = {
        running: false,
        requestedAttempts: 0,
        intervalMs: ACTIVE_STREAM_RECOVERY_INTERVAL_MS,
        reasons: new Set(),
        waiters: [],
      };
      this.states.set(conversationId, state);
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
    const result = await this.runProbe(conversationId, state);
    const waiters = state.waiters.splice(0);
    this.states.delete(conversationId);
    for (const waiter of waiters) waiter.resolve(result);
  }

  private async runProbe(
    conversationId: string,
    state: ProbeState,
  ): Promise<ActiveStreamRecoveryResult> {
    let attempts = 0;

    while (attempts < state.requestedAttempts) {
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
        return { status: 'error', attempts: attempts + 1, attachable: [], error };
      }

      attempts += 1;
      const isAttachable = handlers.isAttachable ?? defaultAttachable;
      const attachable = activeStreams.filter(isAttachable);
      if (attachable.length > 0) {
        const reason = Array.from(state.reasons).join('+') || DEFAULT_REASON;
        try {
          await Promise.all(attachable.map((stream) =>
            handlers.prepareAttach?.(conversationId, stream, reason),
          ));
        } catch (error) {
          return { status: 'error', attempts, attachable, error };
        }

        for (const stream of attachable) {
          handlers.resumeStream?.(conversationId, stream, reason);
        }
        return { status: 'attached', attempts, attachable };
      }

      if (attempts >= state.requestedAttempts) {
        return { status: 'none', attempts, attachable: [] };
      }

      await (handlers.delay ?? defaultDelay)(state.intervalMs);
    }

    return { status: 'none', attempts, attachable: [] };
  }
}
