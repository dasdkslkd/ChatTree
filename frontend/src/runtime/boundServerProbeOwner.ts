import type { BindingEvent } from './bindingState';
import {
  BoundServerIdentityError,
  isFatalBoundServerError,
  sameBoundServerContext,
  type BoundServerContext,
} from './connectionIdentity';

const RETRY_DELAYS_MS = [500, 1000, 2000, 5000] as const;
const HEALTH_PROBE_DELAY_MS = 30000;

export type BoundServerProbeScheduler = Readonly<{
  setTimeout(callback: () => void, delay: number): number;
  clearTimeout(timer: number): void;
}>;

export type BoundServerProbeOwnerOptions = Readonly<{
  probe(signal: AbortSignal): Promise<BoundServerContext>;
  dispatch(event: BindingEvent): void;
  onInitialContext?(context: BoundServerContext): void;
  reloadCurrentPage(): void;
  scheduler: BoundServerProbeScheduler;
}>;

export class BoundServerProbeOwner {
  private readonly options: BoundServerProbeOwnerOptions;
  private started = false;
  private stopped = false;
  private disposed = false;
  private generation = 0;
  private retryIndex = 0;
  private context: BoundServerContext | null = null;
  private controller: AbortController | null = null;
  private timer: number | null = null;

  constructor(options: BoundServerProbeOwnerOptions) {
    this.options = options;
  }

  start(): void {
    if (this.started || this.stopped || this.disposed) return;
    this.started = true;
    this.runProbe();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.stop();
  }

  private runProbe(): void {
    if (this.stopped || this.disposed || this.controller) return;
    const generation = this.generation + 1;
    this.generation = generation;
    const controller = new AbortController();
    this.controller = controller;

    let pending: Promise<BoundServerContext>;
    try {
      pending = this.options.probe(controller.signal);
    } catch (error) {
      pending = Promise.reject(error);
    }
    void pending.then(
      (context) => this.handleReady(generation, controller, context),
      (error: unknown) => this.handleFailure(generation, controller, error),
    );
  }

  private retireAttempt(generation: number, controller: AbortController): boolean {
    if (
      this.stopped
      || this.disposed
      || this.generation !== generation
      || this.controller !== controller
    ) {
      return false;
    }
    this.controller = null;
    controller.abort();
    return true;
  }

  private handleReady(
    generation: number,
    controller: AbortController,
    context: BoundServerContext,
  ): void {
    if (!this.retireAttempt(generation, controller)) return;

    if (!this.context) {
      try {
        this.options.onInitialContext?.(context);
      } catch (error) {
        this.stopWithFatalError(error);
        return;
      }
      if (!this.isLive(generation)) return;
      this.context = context;
    } else if (!sameBoundServerContext(this.context, context)) {
      if (
        this.context.profileId !== context.profileId
        || this.context.apiBase !== context.apiBase
      ) {
        this.stopWithFatalError(new BoundServerIdentityError(
          'Bound frontend cannot change Profile or API base',
        ));
      } else {
        this.stop();
        if (!this.disposed) this.options.reloadCurrentPage();
      }
      return;
    }

    this.options.dispatch({ type: 'probe_ready', context });
    if (!this.isLive(generation)) return;
    this.retryIndex = 0;
    this.schedule(HEALTH_PROBE_DELAY_MS);
  }

  private handleFailure(
    generation: number,
    controller: AbortController,
    error: unknown,
  ): void {
    if (!this.retireAttempt(generation, controller)) return;
    if (isFatalBoundServerError(error)) {
      this.stopWithFatalError(error);
      return;
    }

    this.options.dispatch({ type: 'probe_failed', error });
    if (!this.isLive(generation)) return;
    const delay = RETRY_DELAYS_MS[
      Math.min(this.retryIndex, RETRY_DELAYS_MS.length - 1)
    ];
    this.retryIndex += 1;
    this.schedule(delay);
  }

  private schedule(delay: number): void {
    if (this.stopped || this.disposed || this.timer !== null) return;
    const generation = this.generation;
    this.timer = this.options.scheduler.setTimeout(() => {
      if (
        this.stopped
        || this.disposed
        || this.generation !== generation
      ) {
        return;
      }
      this.timer = null;
      this.runProbe();
    }, delay);
  }

  private isLive(generation: number): boolean {
    return !this.stopped && !this.disposed && this.generation === generation;
  }

  private stopWithFatalError(error: unknown): void {
    if (this.stopped || this.disposed) return;
    this.stop();
    this.options.dispatch({ type: 'fatal_error', error });
  }

  private stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.generation += 1;
    if (this.timer !== null) {
      this.options.scheduler.clearTimeout(this.timer);
      this.timer = null;
    }
    const controller = this.controller;
    this.controller = null;
    controller?.abort();
  }
}
