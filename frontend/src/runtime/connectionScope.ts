import {
  BoundServerLeaseChangedError,
  sameBoundServerContext,
  type BoundServerContext,
} from './connectionIdentity';

export type ConnectionScope = Readonly<{
  profileId: string;
  serverInstanceId: string;
  leaseId: string;
  signal: AbortSignal;
}>;

export class StaleConnectionScopeError extends Error {
  constructor() {
    super('Response belongs to an inactive ChatTree connection scope');
    this.name = 'StaleConnectionScopeError';
  }
}

type InvalidationListener = () => void;

export class ConnectionScopeRuntime {
  private context: BoundServerContext | null = null;
  private scope: ConnectionScope | null = null;
  private controller: AbortController | null = null;
  private readonly listeners = new Set<InvalidationListener>();

  install(context: BoundServerContext): ConnectionScope {
    if (this.context) {
      if (this.scope && sameBoundServerContext(this.context, context)) {
        return this.scope;
      }
      throw new BoundServerLeaseChangedError(
        'Connection scope requires a page reload',
      );
    }
    const controller = new AbortController();
    this.context = context;
    this.controller = controller;
    this.scope = Object.freeze({
      profileId: context.profileId,
      serverInstanceId: context.serverInstanceId,
      leaseId: context.connectionLeaseId,
      signal: controller.signal,
    });
    return this.scope;
  }

  current(): ConnectionScope {
    if (!this.scope || this.scope.signal.aborted) {
      throw new StaleConnectionScopeError();
    }
    return this.scope;
  }

  isActive(scope: ConnectionScope): boolean {
    return this.scope === scope && !scope.signal.aborted;
  }

  invalidate(scope: ConnectionScope = this.current()): boolean {
    if (!this.isActive(scope)) return false;
    this.controller?.abort();
    for (const listener of [...this.listeners]) listener();
    return true;
  }

  subscribeInvalidation(listener: InvalidationListener): () => void {
    this.listeners.add(listener);
    if (this.scope?.signal.aborted) listener();
    return () => this.listeners.delete(listener);
  }
}

export function composeConnectionAbortSignal(
  caller: AbortSignal | null | undefined,
  scope: AbortSignal,
): AbortSignal {
  if (!caller || caller === scope) return scope;
  return AbortSignal.any([caller, scope]);
}

export const connectionScopeRuntime = new ConnectionScopeRuntime();
