import {
  BoundServerLeaseChangedError,
  sameBoundServerContext,
  type BoundServerContext,
} from './connectionIdentity';

export type ConnectionEpochToken = Readonly<{
  profileId: string;
  serverInstanceId: string;
  connectionEpoch: number;
  connectionLeaseId: string;
  generation: number;
}>;

export class StaleConnectionEpochError extends Error {
  constructor() {
    super('Response belongs to a stale ChatTree connection epoch');
    this.name = 'StaleConnectionEpochError';
  }
}

type InvalidationListener = () => void;

export class ConnectionEpochRuntime {
  private context: BoundServerContext | null = null;
  private generation = 0;
  private valid = false;
  private readonly invalidated = new AbortController();
  private readonly listeners = new Set<InvalidationListener>();

  install(next: BoundServerContext): void {
    if (this.context) {
      if (this.valid && sameBoundServerContext(this.context, next)) return;
      throw new BoundServerLeaseChangedError(
        'Connection runtime requires a page reload',
      );
    }
    this.context = next;
    this.valid = true;
    this.generation += 1;
  }

  capture(): ConnectionEpochToken {
    if (!this.context || !this.valid) throw new StaleConnectionEpochError();
    return Object.freeze({
      profileId: this.context.profileId,
      serverInstanceId: this.context.serverInstanceId,
      connectionEpoch: this.context.connectionEpoch,
      connectionLeaseId: this.context.connectionLeaseId,
      generation: this.generation,
    });
  }

  isCurrent(token: ConnectionEpochToken): boolean {
    return this.valid
      && token.generation === this.generation
      && token.connectionEpoch === this.context?.connectionEpoch
      && token.connectionLeaseId === this.context?.connectionLeaseId
      && token.serverInstanceId === this.context?.serverInstanceId
      && token.profileId === this.context?.profileId;
  }

  assertCurrent(token: ConnectionEpochToken): void {
    if (!this.isCurrent(token)) throw new StaleConnectionEpochError();
  }

  signalFor(token: ConnectionEpochToken): AbortSignal {
    if (!this.isCurrent(token)) return AbortSignal.abort();
    return this.invalidated.signal;
  }

  invalidate(token: ConnectionEpochToken): boolean {
    if (!this.isCurrent(token)) return false;
    this.valid = false;
    this.invalidated.abort();
    this.generation += 1;
    for (const listener of [...this.listeners]) listener();
    return true;
  }

  subscribeInvalidation(listener: InvalidationListener): () => void {
    this.listeners.add(listener);
    if (this.context && !this.valid) listener();
    let subscribed = true;
    return () => {
      if (!subscribed) return;
      subscribed = false;
      this.listeners.delete(listener);
    };
  }
}

export function composeConnectionAbortSignal(
  caller: AbortSignal | null | undefined,
  runtime: AbortSignal,
): AbortSignal {
  if (!caller || caller === runtime) return runtime;
  return AbortSignal.any([caller, runtime]);
}

export const connectionEpochRuntime = new ConnectionEpochRuntime();

export const captureConnectionEpoch = (): ConnectionEpochToken => (
  connectionEpochRuntime.capture()
);

export function commitForConnectionEpoch(
  token: ConnectionEpochToken,
  commit: () => void,
): boolean {
  if (!connectionEpochRuntime.isCurrent(token)) return false;
  commit();
  return true;
}
