import type { RunEventPayload, RunRecord } from '../types/run';
import {
  connectionEpochRuntime,
  type ConnectionEpochRuntime,
  type ConnectionEpochToken,
} from './connectionEpoch';
import { getFrontendBootstrap } from './frontendBootstrap';
import {
  buildFrontendRoute,
  readFrontendRouteLocation,
  type FrontendRoute,
  type FrontendRouteLocation,
} from './profileRoute';

export { readFrontendRouteLocation } from './profileRoute';

export type RouteRestoreResult = Readonly<{
  conversationId: string | null;
  nodeId: string | null;
  runId: string | null;
}>;

export type RouteRestoreActions = Readonly<{
  boundProfileId: string;
  selectConversation(id: string, token: ConnectionEpochToken): Promise<boolean>;
  switchNode(id: string, token: ConnectionEpochToken): Promise<boolean>;
  getRun(id: string, token: ConnectionEpochToken): Promise<RunRecord>;
  getEvents(
    id: string,
    fromEvent: number,
    token: ConnectionEpochToken,
  ): Promise<RunEventPayload[]>;
  restoreAndAttachRun(
    run: RunRecord,
    events: RunEventPayload[],
    token: ConnectionEpochToken,
  ): void;
  applyRestoredRoute(
    route: FrontendRoute,
    result: RouteRestoreResult,
    token: ConnectionEpochToken,
  ): void | Promise<void>;
}>;

export type RouteSubmitOptions = Readonly<{
  prepare?: (
    token: ConnectionEpochToken,
    intent: number,
  ) => void | Promise<void>;
  afterRestore?: (
    route: FrontendRoute,
    result: RouteRestoreResult,
    token: ConnectionEpochToken,
    intent: number,
  ) => void | Promise<void>;
}>;

type FrontendRouteInput = FrontendRoute | (() => FrontendRoute);

type RouteEpochGuard = Pick<ConnectionEpochRuntime, 'assertCurrent'>;

export async function waitForRouteReadiness(
  token: ConnectionEpochToken,
  readiness: () => Promise<unknown>,
  options: Readonly<{ timeoutMs: number; cancel: () => void }>,
  epochGuard: RouteEpochGuard = connectionEpochRuntime,
): Promise<boolean> {
  if (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0) {
    throw new Error('Route readiness timeout must be positive');
  }
  epochGuard.assertCurrent(token);
  let timeoutHandle: ReturnType<typeof setTimeout> | null = null;
  try {
    const ready = await Promise.race([
      Promise.resolve().then(readiness).then(() => true),
      new Promise<false>((resolve) => {
        timeoutHandle = setTimeout(() => resolve(false), options.timeoutMs);
      }),
    ]);
    epochGuard.assertCurrent(token);
    if (!ready) {
      options.cancel();
      epochGuard.assertCurrent(token);
    }
    return ready;
  } finally {
    if (timeoutHandle !== null) clearTimeout(timeoutHandle);
  }
}

type RouteRenderScheduler = Readonly<{
  requestFrame(callback: () => void): number;
  cancelFrame(handle: number): void;
  setTimer(callback: () => void, delayMs: number): ReturnType<typeof setTimeout>;
  clearTimer(handle: ReturnType<typeof setTimeout>): void;
}>;

function defaultRouteRenderScheduler(): RouteRenderScheduler {
  return {
    requestFrame: (callback) => window.requestAnimationFrame(callback),
    cancelFrame: (handle) => window.cancelAnimationFrame(handle),
    setTimer: (callback, delayMs) => setTimeout(callback, delayMs),
    clearTimer: (handle) => clearTimeout(handle),
  };
}

export function waitForRouteRender(
  token: ConnectionEpochToken,
  probe: () => boolean,
  options: Readonly<{
    timeoutMs: number;
    signals?: readonly AbortSignal[];
    scheduler?: RouteRenderScheduler;
  }>,
  epochGuard: RouteEpochGuard = connectionEpochRuntime,
): Promise<boolean> {
  if (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0) {
    return Promise.reject(new Error('Route render timeout must be positive'));
  }
  const scheduler = options.scheduler ?? defaultRouteRenderScheduler();
  const signals = options.signals ?? [];

  return new Promise<boolean>((resolve, reject) => {
    let settled = false;
    let frameHandle: number | null = null;
    let timerHandle: ReturnType<typeof setTimeout> | null = null;

    const cleanup = () => {
      if (frameHandle !== null) scheduler.cancelFrame(frameHandle);
      if (timerHandle !== null) scheduler.clearTimer(timerHandle);
      for (const signal of signals) signal.removeEventListener('abort', handleAbort);
      frameHandle = null;
      timerHandle = null;
    };
    const finish = (value: boolean) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };
    const fail = (error: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const assertActive = () => {
      epochGuard.assertCurrent(token);
      if (signals.some((signal) => signal.aborted)) {
        throw new Error('Route render wait cancelled');
      }
    };
    const check = () => {
      frameHandle = null;
      try {
        assertActive();
        if (probe()) {
          finish(true);
          return;
        }
        frameHandle = scheduler.requestFrame(check);
      } catch (error) {
        fail(error);
      }
    };
    function handleAbort() {
      try {
        assertActive();
        fail(new Error('Route render wait cancelled'));
      } catch (error) {
        fail(error);
      }
    }

    try {
      assertActive();
      for (const signal of signals) signal.addEventListener('abort', handleAbort, { once: true });
      timerHandle = scheduler.setTimer(() => {
        try {
          assertActive();
          finish(false);
        } catch (error) {
          fail(error);
        }
      }, options.timeoutMs);
      check();
    } catch (error) {
      fail(error);
    }
  });
}

async function guardedRouteAction<T>(
  token: ConnectionEpochToken,
  action: () => T | Promise<T>,
  epochGuard: RouteEpochGuard,
): Promise<T> {
  epochGuard.assertCurrent(token);
  const value = await action();
  epochGuard.assertCurrent(token);
  return value;
}

async function guardedRequiredRouteAction(
  token: ConnectionEpochToken,
  action: () => boolean | Promise<boolean>,
  epochGuard: RouteEpochGuard,
  description: string,
): Promise<void> {
  const succeeded = await guardedRouteAction(token, action, epochGuard);
  if (!succeeded) throw new Error(`Route ${description} failed`);
}

export function assertBoundFrontendRoute(
  route: FrontendRoute,
  boundProfileId: string,
): void {
  if (route.profileId !== boundProfileId) {
    throw new Error('Route does not match bound Profile');
  }
}

export async function restoreBoundFrontendRoute(
  route: FrontendRoute,
  actions: RouteRestoreActions,
  token: ConnectionEpochToken,
  epochGuard: RouteEpochGuard = connectionEpochRuntime,
): Promise<RouteRestoreResult> {
  epochGuard.assertCurrent(token);
  assertBoundFrontendRoute(route, actions.boundProfileId);

  let result: RouteRestoreResult;
  if (route.kind === 'profile') {
    result = { conversationId: null, nodeId: null, runId: null };
  } else if (route.kind === 'conversation') {
    await guardedRequiredRouteAction(
      token,
      () => actions.selectConversation(route.conversationId, token),
      epochGuard,
      'conversation selection',
    );
    result = { conversationId: route.conversationId, nodeId: null, runId: null };
  } else if (route.kind === 'node') {
    await guardedRequiredRouteAction(
      token,
      () => actions.selectConversation(route.conversationId, token),
      epochGuard,
      'conversation selection',
    );
    await guardedRequiredRouteAction(
      token,
      () => actions.switchNode(route.nodeId, token),
      epochGuard,
      'node selection',
    );
    result = {
      conversationId: route.conversationId,
      nodeId: route.nodeId,
      runId: null,
    };
  } else {
    const run = await guardedRouteAction(
      token,
      () => actions.getRun(route.runId, token),
      epochGuard,
    );
    await guardedRequiredRouteAction(
      token,
      () => actions.selectConversation(run.conversation_id, token),
      epochGuard,
      'run conversation selection',
    );
    const events = await guardedRouteAction(
      token,
      () => actions.getEvents(route.runId, 0, token),
      epochGuard,
    );
    await guardedRouteAction(
      token,
      () => actions.restoreAndAttachRun(run, events, token),
      epochGuard,
    );
    result = {
      conversationId: run.conversation_id,
      nodeId: null,
      runId: route.runId,
    };
  }

  await guardedRouteAction(
    token,
    () => actions.applyRestoredRoute(route, result, token),
    epochGuard,
  );
  return result;
}

export class BoundRouteRestorer {
  private tail: Promise<void> = Promise.resolve();
  private nextIntent = 0;
  private disposed = false;
  private readonly disposeController = new AbortController();
  private readonly actions: RouteRestoreActions;
  private readonly token: ConnectionEpochToken;
  private readonly epochGuard: RouteEpochGuard;
  private readonly ownerGuard: RouteEpochGuard;

  constructor(
    actions: RouteRestoreActions,
    token: ConnectionEpochToken,
    epochGuard: RouteEpochGuard = connectionEpochRuntime,
  ) {
    this.actions = actions;
    this.token = token;
    this.epochGuard = epochGuard;
    this.ownerGuard = {
      assertCurrent: (candidate) => {
        this.epochGuard.assertCurrent(candidate);
        if (this.disposed) throw new Error('Frontend route owner is disposed');
      },
    };
  }

  dispose(): void {
    this.disposed = true;
    this.disposeController.abort();
  }

  get signal(): AbortSignal {
    return this.disposeController.signal;
  }

  run<T>(
    operation: (token: ConnectionEpochToken, intent: number) => T | Promise<T>,
  ): Promise<T> {
    const intent = ++this.nextIntent;
    const request = this.tail.then(() => guardedRouteAction(
      this.token,
      () => operation(this.token, intent),
      this.ownerGuard,
    ));
    this.tail = request.then(() => undefined, () => undefined);
    return request;
  }

  submit(
    routeInput: FrontendRouteInput,
    options: RouteSubmitOptions = {},
  ): Promise<RouteRestoreResult> {
    return this.run(async (token, intent) => {
      if (options.prepare) {
        await guardedRouteAction(
          token,
          () => options.prepare!(token, intent),
          this.ownerGuard,
        );
      }
      this.ownerGuard.assertCurrent(token);
      const route = typeof routeInput === 'function' ? routeInput() : routeInput;
      const result = await restoreBoundFrontendRoute(
        route,
        this.actions,
        token,
        this.ownerGuard,
      );
      if (options.afterRestore) {
        await guardedRouteAction(
          token,
          () => options.afterRestore!(route, result, token, intent),
          this.ownerGuard,
        );
      }
      return result;
    });
  }
}

type FrontendHistory = Pick<History, 'pushState' | 'replaceState'>;

export function createBoundFrontendNavigator(
  boundProfileId: string,
  history: FrontendHistory,
): (route: FrontendRoute, mode: 'push' | 'replace') => void {
  return (route, mode) => {
    assertBoundFrontendRoute(route, boundProfileId);
    const path = buildFrontendRoute(route);
    if (mode === 'push') history.pushState(null, '', path);
    else history.replaceState(null, '', path);
  };
}

export function navigateBoundFrontend(
  route: FrontendRoute,
  mode: 'push' | 'replace',
): void {
  const bound = getFrontendBootstrap();
  createBoundFrontendNavigator(bound.profileId, window.history)(route, mode);
}

type PopstateTarget = Readonly<{
  location: FrontendRouteLocation;
  addEventListener(type: 'popstate', listener: () => void): void;
  removeEventListener(type: 'popstate', listener: () => void): void;
}>;

export function bindBoundFrontendPopstate(
  target: PopstateTarget,
  boundProfileId: string,
  restorer: Pick<BoundRouteRestorer, 'submit'>,
  reportError: (error: unknown) => void,
  commitLocation?: (route: FrontendRoute) => void,
): () => void {
  const restoreLocation = () => {
    let route: FrontendRoute;
    try {
      route = readFrontendRouteLocation(target.location);
      assertBoundFrontendRoute(route, boundProfileId);
    } catch (error) {
      reportError(error);
      return;
    }
    void restorer.submit(route, commitLocation
      ? { afterRestore: () => commitLocation(route) }
      : undefined).catch(reportError);
  };
  target.addEventListener('popstate', restoreLocation);
  return () => target.removeEventListener('popstate', restoreLocation);
}
