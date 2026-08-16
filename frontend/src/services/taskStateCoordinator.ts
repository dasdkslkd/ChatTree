import {
  storeTaskState,
  taskStateApi,
  type TaskStateSnapshot,
} from '../api/taskState';

const TASK_STATE_REFRESH_BACKOFF_MS = [1000, 2000, 4000, 8000, 10000] as const;

type TaskStateListener = (state: TaskStateSnapshot) => void;

type TaskStateApiLike = {
  fetch: (conversationId: string) => Promise<TaskStateSnapshot>;
};

type TaskStateEntry = {
  state: TaskStateSnapshot | null;
  listeners: Set<TaskStateListener>;
  inFlight: Promise<TaskStateSnapshot> | null;
  dirty: boolean;
  timer: ReturnType<typeof setTimeout> | null;
  attempt: number;
  lastRefreshedVersion: string | null;
};

function shouldContinueRefreshing(state: TaskStateSnapshot): boolean {
  return state.flags.running;
}

export class TaskStateCoordinator {
  private readonly entries = new Map<string, TaskStateEntry>();

  private readonly api: TaskStateApiLike;

  constructor(api: TaskStateApiLike = taskStateApi) {
    this.api = api;
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

  async refresh(conversationId: string): Promise<TaskStateSnapshot> {
    const entry = this.ensureEntry(conversationId);
    if (entry.inFlight) {
      entry.dirty = true;
      return entry.inFlight;
    }

    this.clearTimer(entry);
    entry.inFlight = this.drainRefresh(conversationId, entry);
    try {
      return await entry.inFlight;
    } finally {
      entry.inFlight = null;
      if (entry.dirty) {
        void this.refresh(conversationId);
      } else {
        this.scheduleNext(conversationId, entry);
      }
    }
  }

  async invalidate(conversationId: string): Promise<TaskStateSnapshot> {
    const entry = this.ensureEntry(conversationId);
    entry.dirty = true;
    entry.attempt = 0;
    this.clearTimer(entry);
    return this.refresh(conversationId);
  }

  apply(conversationId: string, state: TaskStateSnapshot): void {
    storeTaskState(conversationId, state);
    const entry = this.ensureEntry(conversationId);
    entry.state = state;
    entry.dirty = false;
    entry.attempt = 0;
    this.notify(entry, state);
    this.scheduleNext(conversationId, entry);
  }

  clear(conversationId: string): void {
    const entry = this.entries.get(conversationId);
    if (!entry) return;
    this.clearTimer(entry);
    this.entries.delete(conversationId);
    taskStateApi.clear(conversationId);
  }

  private async drainRefresh(conversationId: string, entry: TaskStateEntry): Promise<TaskStateSnapshot> {
    let latest: TaskStateSnapshot | null = null;
    do {
      entry.dirty = false;
      latest = await this.api.fetch(conversationId);
      entry.state = latest;
      this.notify(entry, latest);
    } while (entry.dirty);
    return latest;
  }

  private scheduleNext(conversationId: string, entry: TaskStateEntry): void {
    this.clearTimer(entry);
    const state = entry.state;
    const changed = state !== null && state.version !== entry.lastRefreshedVersion;
    entry.lastRefreshedVersion = state?.version ?? null;
    if (!state || !changed || !shouldContinueRefreshing(state) || entry.listeners.size === 0) {
      entry.attempt = 0;
      return;
    }
    const delay = TASK_STATE_REFRESH_BACKOFF_MS[Math.min(entry.attempt, TASK_STATE_REFRESH_BACKOFF_MS.length - 1)];
    entry.attempt += 1;
    entry.timer = setTimeout(() => {
      entry.timer = null;
      void this.refresh(conversationId);
    }, delay);
  }

  private notify(entry: TaskStateEntry, state: TaskStateSnapshot): void {
    for (const listener of entry.listeners) listener(state);
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
        lastRefreshedVersion: null,
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
}

export const taskStateCoordinator = new TaskStateCoordinator();
