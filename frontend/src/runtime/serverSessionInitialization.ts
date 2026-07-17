export type ServerSessionStoreInitializer = Readonly<{
  loadConfig(): Promise<void>;
  getConfig(): unknown | null;
  loadProviders(): Promise<void>;
  getError(): unknown | null;
}>;

export async function initializeServerSessionStores(
  store: ServerSessionStoreInitializer,
): Promise<void> {
  await store.loadConfig();
  if (store.getConfig() == null) {
    throw new Error('Frontend config initialization failed');
  }

  await store.loadProviders();
  const storeError = store.getError();
  if (storeError) {
    throw storeError instanceof Error ? storeError : new Error(String(storeError));
  }
}

export type ServerSessionInitializationScheduler = Readonly<{
  setTimeout(callback: () => void, delay: number): number;
  clearTimeout(timerId: number): void;
}>;

export type ServerSessionInitializationOwnerOptions = Readonly<{
  initialize(): Promise<void>;
  scheduler: ServerSessionInitializationScheduler;
  onError?(error: unknown): void;
}>;

const RETRY_INTERVAL_MS = 30_000;
const defaultOnError = (error: unknown) => {
  console.error('Failed to initialize frontend data', error);
};

export class ServerSessionInitializationOwner {
  private readonly initialize: () => Promise<void>;
  private readonly scheduler: ServerSessionInitializationScheduler;
  private readonly onError: (error: unknown) => void;
  private connected = false;
  private started = false;
  private disposed = false;
  private inFlight = false;
  private complete = false;
  private timerId: number | null = null;
  private generation = 0;

  constructor(options: ServerSessionInitializationOwnerOptions) {
    this.initialize = options.initialize;
    this.scheduler = options.scheduler;
    this.onError = options.onError ?? defaultOnError;
  }

  start(): void {
    if (this.disposed || this.started) return;
    this.started = true;
    if (this.connected) this.startAttempt();
  }

  setConnected(connected: boolean): void {
    if (this.disposed) return;
    const wasConnected = this.connected;
    this.connected = connected;
    if (!connected) {
      this.clearRetry();
      return;
    }
    if (!wasConnected && this.started && !this.inFlight && !this.complete) {
      this.startAttempt();
    }
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.generation += 1;
    this.clearRetry();
  }

  private startAttempt(): void {
    if (this.disposed || !this.started || !this.connected || this.inFlight || this.complete) return;
    this.clearRetry();
    this.inFlight = true;
    const generation = this.generation;
    let result: Promise<void>;
    try {
      result = this.initialize();
    } catch (error) {
      result = Promise.reject(error);
    }
    void Promise.resolve(result).then(
      () => this.finishAttempt(generation, true),
      error => this.finishAttempt(generation, false, error),
    );
  }

  private finishAttempt(generation: number, succeeded: boolean, error?: unknown): void {
    if (this.disposed || generation !== this.generation) return;
    this.inFlight = false;
    if (succeeded) {
      this.complete = true;
      this.clearRetry();
      return;
    }

    this.onError(error);
    if (!this.connected || this.timerId !== null) return;
    this.timerId = this.scheduler.setTimeout(() => {
      this.timerId = null;
      this.startAttempt();
    }, RETRY_INTERVAL_MS);
  }

  private clearRetry(): void {
    if (this.timerId === null) return;
    this.scheduler.clearTimeout(this.timerId);
    this.timerId = null;
  }
}
