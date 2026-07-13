import type { FrontendPerfEvent, PerfConfig } from './types';

const DEFAULT_CONFIG: PerfConfig = {
  enabled: false,
  perf_run_id: '',
  sample_rate: 1,
  max_attr_length: 512,
  max_batch_events: 500,
};

let config: PerfConfig = DEFAULT_CONFIG;
let initialized = false;
let flushing = false;
let queue: FrontendPerfEvent[] = [];

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

export async function loadPerfConfig(): Promise<PerfConfig> {
  if (!hasFetch()) {
    initialized = true;
    config = DEFAULT_CONFIG;
    return config;
  }
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  const timeout = controller ? globalThis.setTimeout(() => controller.abort(), 2000) : null;
  try {
    const response = await fetch('/api/perf/config', {
      method: 'GET',
      signal: controller?.signal,
    });
    if (!response.ok) throw new Error(`perf config ${response.status}`);
    const data = await response.json();
    config = {
      ...DEFAULT_CONFIG,
      ...data,
      enabled: Boolean(data?.enabled),
      sample_rate: Number.isFinite(data?.sample_rate) ? Number(data.sample_rate) : 1,
      max_attr_length: Number.isFinite(data?.max_attr_length) ? Number(data.max_attr_length) : 512,
      max_batch_events: Number.isFinite(data?.max_batch_events) ? Number(data.max_batch_events) : 500,
    };
  } catch {
    config = DEFAULT_CONFIG;
  } finally {
    if (timeout !== null) globalThis.clearTimeout(timeout);
    initialized = true;
  }
  return config;
}

export function getPerfConfig(): PerfConfig {
  return config;
}

export function isPerfEnabled(): boolean {
  return initialized && config.enabled;
}

export function recordFrontendEvent(event: FrontendPerfEvent): void {
  if (!isPerfEnabled()) return;
  if (config.sample_rate < 1 && Math.random() > config.sample_rate) return;
  queue.push(sanitizeEvent(event));
  if (queue.length >= Math.min(50, config.max_batch_events)) {
    void flushPerfEvents();
  }
}

export async function flushPerfEvents(): Promise<void> {
  if (!isPerfEnabled() || flushing || !hasFetch() || queue.length === 0) return;
  flushing = true;
  const batch = queue.splice(0, config.max_batch_events);
  try {
    await fetch('/api/perf/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: batch }),
    });
  } catch {
    // Drop failed perf batches. Telemetry must not disturb the app.
  } finally {
    flushing = false;
  }
}

export function flushPerfEventsSync(): boolean {
  if (!isPerfEnabled() || queue.length === 0) return false;
  const batch = queue.splice(0, config.max_batch_events);
  const body = JSON.stringify({ events: batch });
  const nav = typeof navigator !== 'undefined' ? navigator : null;
  if (nav && typeof nav.sendBeacon === 'function') {
    try {
      const ok = nav.sendBeacon('/api/perf/events', new Blob([body], { type: 'application/json' }));
      if (ok) return true;
    } catch {
      // Fall through to fetch keepalive.
    }
  }
  if (hasFetch()) {
    try {
      void fetch('/api/perf/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        keepalive: true,
      });
      return true;
    } catch {
      // Telemetry must not disturb app teardown.
    }
  }
  queue = batch.concat(queue);
  return false;
}

export function resetPerfForTests(nextConfig: Partial<PerfConfig> = {}): void {
  config = { ...DEFAULT_CONFIG, ...nextConfig };
  initialized = true;
  queue = [];
  flushing = false;
}
