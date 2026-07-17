import {
  storeTaskState,
  taskStateApi,
  type TaskStateSnapshot,
} from '../api/taskState';
import {
  StaleConnectionEpochError,
  connectionEpochRuntime,
  type ConnectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';

const TASK_STATE_REFRESH_BACKOFF_MS = [1000, 2000, 4000, 8000, 10000] as const;

type TaskStateListener = (state: TaskStateSnapshot) => void;

type TaskStateApiLike = {
  fetch: (conversationId: string, token?: ConnectionEpochToken) => Promise<TaskStateSnapshot>;
};

type TaskStateEpochSource = Pick<ConnectionEpochRuntime, 'capture' | 'isCurrent'>;

type StoreTaskState = typeof storeTaskState;

type TaskStateEntry = {
  state: TaskStateSnapshot | null;
  listeners: Set<TaskStateListener>;
  inFlight: Promise<TaskStateSnapshot> | null;
  dirty: boolean;
  timer: ReturnType<typeof setTimeout> | null;
  attempt: number;
  ownerToken: ConnectionEpochToken | null;
};

function shouldContinueRefreshing(state: TaskStateSnapshot): boolean {
  return state.flags.running || state.flags.delivering;
}

export class TaskStateCoordinator {
  private readonly entries = new Map<string, TaskStateEntry>();

  private readonly api: TaskStateApiLike;

  private readonly epochSource: TaskStateEpochSource;

  private readonly storeState: StoreTaskState;

  constructor(
    api: TaskStateApiLike = taskStateApi,
    epochSource: TaskStateEpochSource = connectionEpochRuntime,
    storeState: StoreTaskState = storeTaskState,
  ) {
    this.api = api;
    this.epochSource = epochSource;
    this.storeState = storeState;
  }

  subscribe(conversationId: string, listener: TaskStateListener): () => void {
    const entry = this.ensureEntry(conversationId);
    entry.listeners.add(listener);
    if (entry.state) listener(entry.state);
    return () => {
      const current = this.entries.get(conversationId);
      if (!current) return;
      current.listeners.delete(listener);
      if (current.listeners.size === 0 && !current.inFlight) {
        this.clearTimer(current);
      }
    };
  }

  async refresh(
    conversationId: string,
    ownerToken?: ConnectionEpochToken,
  ): Promise<TaskStateSnapshot> {
    const token = this.resolveToken(ownerToken);
    const entry = this.ensureEntry(conversationId);
    if (entry.inFlight) {
      this.assertCurrent(token);
      entry.dirty = true;
      return entry.inFlight;
    }

    this.clearTimer(entry);
    entry.ownerToken = token;
    entry.inFlight = this.drainRefresh(conversationId, entry, token);
    const ownedFlight = entry.inFlight;
    try {
      return await ownedFlight;
    } finally {
      if (entry.inFlight === ownedFlight) {
        entry.inFlight = null;
        if (entry.dirty) {
          this.refreshInBackground(conversationId, token);
        } else {
          this.scheduleNext(conversationId, entry, token);
        }
      }
    }
  }

  async invalidate(
    conversationId: string,
    ownerToken?: ConnectionEpochToken,
  ): Promise<TaskStateSnapshot> {
    const token = this.resolveToken(ownerToken);
    const entry = this.ensureEntry(conversationId);
    this.assertCurrent(token);
    entry.dirty = true;
    entry.attempt = 0;
    this.clearTimer(entry);
    return this.refresh(conversationId, token);
  }

  apply(
    conversationId: string,
    state: TaskStateSnapshot,
    ownerToken?: ConnectionEpochToken,
  ): void {
    const token = this.resolveToken(ownerToken);
    this.assertCurrent(token);
    this.storeState(conversationId, state, undefined, token);
    this.assertCurrent(token);
    const entry = this.ensureEntry(conversationId);
    entry.state = state;
    entry.dirty = false;
    entry.attempt = 0;
    entry.ownerToken = token;
    this.notify(entry, state, token);
    this.assertCurrent(token);
    this.scheduleNext(conversationId, entry, token);
  }

  clear(conversationId: string): void {
    const entry = this.entries.get(conversationId);
    if (!entry) return;
    this.clearTimer(entry);
    this.entries.delete(conversationId);
    taskStateApi.clear(conversationId);
  }

  private async drainRefresh(
    conversationId: string,
    entry: TaskStateEntry,
    token: ConnectionEpochToken,
  ): Promise<TaskStateSnapshot> {
    let latest: TaskStateSnapshot | null = null;
    do {
      this.assertCurrent(token);
      entry.dirty = false;
      latest = await this.api.fetch(conversationId, token);
      this.assertCurrent(token);
      if (entry.ownerToken !== token) throw new StaleConnectionEpochError();
      entry.state = latest;
      this.notify(entry, latest, token);
    } while (entry.dirty);
    return latest;
  }

  private scheduleNext(
    conversationId: string,
    entry: TaskStateEntry,
    token: ConnectionEpochToken,
  ): void {
    this.clearTimer(entry);
    if (!this.epochSource.isCurrent(token) || entry.ownerToken !== token) return;
    if (!entry.state || !shouldContinueRefreshing(entry.state) || entry.listeners.size === 0) {
      entry.attempt = 0;
      return;
    }
    const delay = TASK_STATE_REFRESH_BACKOFF_MS[Math.min(entry.attempt, TASK_STATE_REFRESH_BACKOFF_MS.length - 1)];
    entry.attempt += 1;
    entry.timer = setTimeout(() => {
      entry.timer = null;
      this.refreshInBackground(conversationId, token);
    }, delay);
  }

  private notify(
    entry: TaskStateEntry,
    state: TaskStateSnapshot,
    token: ConnectionEpochToken,
  ): void {
    for (const listener of entry.listeners) {
      this.assertCurrent(token);
      listener(state);
    }
  }

  private ensureEntry(conversationId: string): TaskStateEntry {
    let entry = this.entries.get(conversationId);
    if (!entry) {
      entry = {
        state: null,
        listeners: new Set(),
        inFlight: null,
        dirty: false,
        timer: null,
        attempt: 0,
        ownerToken: null,
      };
      this.entries.set(conversationId, entry);
    }
    return entry;
  }

  private clearTimer(entry: TaskStateEntry): void {
    if (entry.timer === null) return;
    clearTimeout(entry.timer);
    entry.timer = null;
  }

  private resolveToken(ownerToken?: ConnectionEpochToken): ConnectionEpochToken {
    const token = ownerToken ?? this.epochSource.capture();
    this.assertCurrent(token);
    return token;
  }

  private assertCurrent(token: ConnectionEpochToken): void {
    if (!this.epochSource.isCurrent(token)) throw new StaleConnectionEpochError();
  }

  private refreshInBackground(conversationId: string, token: ConnectionEpochToken): void {
    void this.refresh(conversationId, token).catch(() => {
      // Polling is best-effort; stale and transport failures must not reject globally.
    });
  }
}

export const taskStateCoordinator = new TaskStateCoordinator();
