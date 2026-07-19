import type { FrontendPerfEvent, PerfConfig } from './types';
import { leaseGuardedFetch } from '../api/leaseFetch';

const DEFAULT_CONFIG: PerfConfig = {
  enabled: false,
  perf_run_id: '',
  sample_rate: 1,
  max_attr_length: 512,
  max_batch_events: 500,
};

const DEFAULT_FLUSH_INTERVAL_MS = 1000;
const MAX_PRE_INIT_EVENTS = DEFAULT_CONFIG.max_batch_events;
const IMMEDIATE_FLUSH_EVENTS = new Set(['stream.done']);
const CRITICAL_EVENTS = new Set([
  'stream.fetch',
  'stream.response_headers',
  'stream.first_bytes',
  'stream.reader_read',
  'stream.done',
]);

let config: PerfConfig = DEFAULT_CONFIG;
let initialized = false;
let flushing = false;
let queue: FrontendPerfEvent[] = [];
let preInitQueue: FrontendPerfEvent[] = [];
let flushTimer: ReturnType<typeof globalThis.setTimeout> | null = null;
let configLoadPromise: Promise<PerfConfig> | null = null;

function hasFetch(): boolean {
  return typeof fetch === 'function';
}

function sanitizeValue(value: unknown): unknown {
  if (typeof value === 'string' && value.length > config.max_attr_length) {
    return `${value.slice(0, config.max_attr_length)}...[len=${value.length}]`;
  }
  if (typeof value === 'number' || typeof value === 'boolean' || value == null) {
    return value;
  }
  if (Array.isArray(value)) return `[array:${value.length}]`;
  if (typeof value === 'object') return '[object]';
  return String(value);
}

function sanitizeEvent(event: FrontendPerfEvent): FrontendPerfEvent {
  const attrs: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(event.attrs || {})) {
    attrs[key.slice(0, 80)] = sanitizeValue(value);
  }
  return {
    ...event,
    attrs,
  };
}

function batchLimit(): number {
  return Math.max(1, config.max_batch_events || DEFAULT_CONFIG.max_batch_events);
}

function clearScheduledFlush(): void {
  if (flushTimer === null) return;
  globalThis.clearTimeout(flushTimer);
  flushTimer = null;
}

function scheduleFlush(): void {
  if (!isPerfEnabled() || queue.length === 0 || flushTimer !== null) return;
  flushTimer = globalThis.setTimeout(() => {
    flushTimer = null;
    void flushPerfEvents();
  }, DEFAULT_FLUSH_INTERVAL_MS);
}

function enqueuePreInitEvent(event: FrontendPerfEvent): void {
  if (preInitQueue.length >= MAX_PRE_INIT_EVENTS) {
    preInitQueue.shift();
  }
  preInitQueue.push(event);
}

function drainPreInitEvents(): void {
  const pending = preInitQueue;
  preInitQueue = [];
  if (!isPerfEnabled() || pending.length === 0) return;
  for (const event of pending) {
    recordFrontendEvent(event);
  }
}

export async function loadPerfConfig(): Promise<PerfConfig> {
  if (initialized) return config;
  if (configLoadPromise) return configLoadPromise;
  if (!hasFetch()) {
    initialized = true;
    config = DEFAULT_CONFIG;
    preInitQueue = [];
    return config;
  }
  configLoadPromise = (async () => {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timeout = controller ? globalThis.setTimeout(() => controller.abort(), 2000) : null;
    try {
      const response = await leaseGuardedFetch('/perf/config', {
        method: 'GET',
        signal: controller?.signal,
      });
      const data = await response.json();
      config = {
        ...DEFAULT_CONFIG,
        ...data,
        enabled: Boolean(data?.enabled),
        sample_rate: Number.isFinite(data?.sample_rate) ? Number(data.sample_rate) : 1,
        max_attr_length: Number.isFinite(data?.max_attr_length) ? Number(data.max_attr_length) : 512,
        max_batch_events: Number.isFinite(data?.max_batch_events) ? Number(data.max_batch_events) : 500,
      };
      initialized = true;
      if (!config.enabled) preInitQueue = [];
      drainPreInitEvents();
    } catch {
      config = DEFAULT_CONFIG;
      initialized = false;
    } finally {
      if (timeout !== null) globalThis.clearTimeout(timeout);
      configLoadPromise = null;
    }
    return config;
  })();
  return configLoadPromise;
}

export function getPerfConfig(): PerfConfig {
  return config;
}

export function isPerfEnabled(): boolean {
  return initialized && config.enabled;
}

export function recordFrontendEvent(event: FrontendPerfEvent): void {
  if (!initialized) {
    enqueuePreInitEvent(event);
    void loadPerfConfig();
    return;
  }
  if (!isPerfEnabled()) return;
  if (config.sample_rate < 1 && !CRITICAL_EVENTS.has(event.name) && Math.random() > config.sample_rate) return;
  queue.push(sanitizeEvent(event));
  if (IMMEDIATE_FLUSH_EVENTS.has(event.name) || queue.length >= batchLimit()) {
    void flushPerfEvents();
    return;
  }
  scheduleFlush();
}

export async function flushPerfEvents(): Promise<void> {
  if (!isPerfEnabled() || !hasFetch() || queue.length === 0) return;
  if (flushing) {
    scheduleFlush();
    return;
  }
  clearScheduledFlush();
  flushing = true;
  const batch = queue.splice(0, batchLimit());
  try {
    await leaseGuardedFetch('/perf/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: batch }),
    });
  } catch {
    // Drop failed perf batches. Telemetry must not disturb the app.
  } finally {
    flushing = false;
    if (queue.length >= batchLimit()) {
      void flushPerfEvents();
    } else if (queue.length > 0) {
      scheduleFlush();
    }
  }
}

export function flushPerfEventsSync(): boolean {
  if (!isPerfEnabled() || queue.length === 0) return false;
  clearScheduledFlush();
  const pending = queue;
  queue = [];
  while (pending.length > 0) {
    const batch = pending.splice(0, batchLimit());
    const body = JSON.stringify({ events: batch });
    if (hasFetch()) {
      try {
        void leaseGuardedFetch('/perf/events', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
          keepalive: true,
        }).catch(() => {});
        continue;
      } catch {
        // Telemetry must not disturb app teardown.
      }
    }
    queue = batch.concat(pending, queue);
    scheduleFlush();
    return false;
  }
  return true;
}

export function resetPerfForTests(
  nextConfig: Partial<PerfConfig> = {},
  options: { initialized?: boolean } = {},
): void {
  clearScheduledFlush();
  config = { ...DEFAULT_CONFIG, ...nextConfig };
  initialized = options.initialized ?? true;
  configLoadPromise = null;
  queue = [];
  preInitQueue = [];
  flushing = false;
}
