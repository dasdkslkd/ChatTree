import type { FrontendPerfEvent, PerfConfig } from './types';
import { leaseGuardedFetch } from '../api/leaseFetch';
import {
  StaleConnectionEpochError,
  captureConnectionEpoch,
  connectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';

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
let configLoadToken: ConnectionEpochToken | null = null;

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

function sameEpochToken(
  left: ConnectionEpochToken | null,
  right: ConnectionEpochToken,
): boolean {
  return Boolean(left
    && left.generation === right.generation
    && left.profileId === right.profileId
    && left.serverInstanceId === right.serverInstanceId
    && left.connectionEpoch === right.connectionEpoch
    && left.connectionLeaseId === right.connectionLeaseId);
}

function resolveEpochToken(token?: ConnectionEpochToken): ConnectionEpochToken {
  if (token) {
    connectionEpochRuntime.assertCurrent(token);
    return token;
  }
  return captureConnectionEpoch();
}

function isStaleEpoch(error: unknown, token: ConnectionEpochToken | null): boolean {
  return !token
    || error instanceof StaleConnectionEpochError
    || !connectionEpochRuntime.isCurrent(token);
}

function scheduleFlush(ownerToken?: ConnectionEpochToken): void {
  if (!isPerfEnabled() || queue.length === 0 || flushTimer !== null) return;
  let token: ConnectionEpochToken;
  try {
    token = resolveEpochToken(ownerToken);
  } catch {
    return;
  }
  const timer = globalThis.setTimeout(() => {
    if (flushTimer === timer) flushTimer = null;
    if (!connectionEpochRuntime.isCurrent(token)) return;
    void flushPerfEvents(token).catch(() => {});
  }, DEFAULT_FLUSH_INTERVAL_MS);
  flushTimer = timer;
}

function enqueuePreInitEvent(event: FrontendPerfEvent): void {
  if (preInitQueue.length >= MAX_PRE_INIT_EVENTS) {
    preInitQueue.shift();
  }
  preInitQueue.push(event);
}

function drainPreInitEvents(token: ConnectionEpochToken): void {
  connectionEpochRuntime.assertCurrent(token);
  const pending = preInitQueue;
  preInitQueue = [];
  if (!isPerfEnabled() || pending.length === 0) return;
  for (const event of pending) {
    recordFrontendEvent(event, token);
  }
}

export async function loadPerfConfig(ownerToken?: ConnectionEpochToken): Promise<PerfConfig> {
  let token: ConnectionEpochToken | null = null;
  try {
    token = resolveEpochToken(ownerToken);
  } catch (error) {
    if (isStaleEpoch(error, token)) return config;
    throw error;
  }
  if (initialized) return config;
  if (configLoadPromise && sameEpochToken(configLoadToken, token)) return configLoadPromise;
  if (!hasFetch()) {
    connectionEpochRuntime.assertCurrent(token);
    initialized = true;
    config = DEFAULT_CONFIG;
    preInitQueue = [];
    return config;
  }

  let ownedPromise: Promise<PerfConfig> | null = null;
  const promise = (async () => {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timeout = controller ? globalThis.setTimeout(() => controller.abort(), 2000) : null;
    try {
      const response = await leaseGuardedFetch('/perf/config', {
        method: 'GET',
        signal: controller?.signal,
      }, token!);
      connectionEpochRuntime.assertCurrent(token!);
      if (!response.ok) throw new Error(`perf config ${response.status}`);
      const data = await response.json();
      connectionEpochRuntime.assertCurrent(token!);
      const nextConfig = {
        ...DEFAULT_CONFIG,
        ...data,
        enabled: Boolean(data?.enabled),
        sample_rate: Number.isFinite(data?.sample_rate) ? Number(data.sample_rate) : 1,
        max_attr_length: Number.isFinite(data?.max_attr_length) ? Number(data.max_attr_length) : 512,
        max_batch_events: Number.isFinite(data?.max_batch_events) ? Number(data.max_batch_events) : 500,
      };
      connectionEpochRuntime.assertCurrent(token!);
      config = nextConfig;
      initialized = true;
      if (!config.enabled) preInitQueue = [];
      drainPreInitEvents(token!);
    } catch (error) {
      if (isStaleEpoch(error, token)) return config;
      connectionEpochRuntime.assertCurrent(token!);
      config = DEFAULT_CONFIG;
      initialized = false;
    } finally {
      if (timeout !== null) globalThis.clearTimeout(timeout);
      if (configLoadPromise === ownedPromise) {
        configLoadPromise = null;
        configLoadToken = null;
      }
    }
    return config;
  })();
  ownedPromise = promise;
  configLoadToken = token;
  configLoadPromise = promise;
  return promise;
}

export function getPerfConfig(): PerfConfig {
  return config;
}

export function isPerfEnabled(): boolean {
  return initialized && config.enabled;
}

export function recordFrontendEvent(
  event: FrontendPerfEvent,
  ownerToken?: ConnectionEpochToken,
): void {
  let token: ConnectionEpochToken;
  try {
    token = resolveEpochToken(ownerToken);
  } catch {
    return;
  }
  if (!initialized) {
    enqueuePreInitEvent(event);
    void loadPerfConfig(token).catch(() => {});
    return;
  }
  if (!isPerfEnabled()) return;
  if (config.sample_rate < 1 && !CRITICAL_EVENTS.has(event.name) && Math.random() > config.sample_rate) return;
  queue.push(sanitizeEvent(event));
  if (IMMEDIATE_FLUSH_EVENTS.has(event.name) || queue.length >= batchLimit()) {
    void flushPerfEvents(token).catch(() => {});
    return;
  }
  scheduleFlush(token);
}

export async function flushPerfEvents(ownerToken?: ConnectionEpochToken): Promise<void> {
  let token: ConnectionEpochToken | null = null;
  try {
    token = resolveEpochToken(ownerToken);
  } catch (error) {
    if (isStaleEpoch(error, token)) return;
    throw error;
  }
  if (!isPerfEnabled() || !hasFetch() || queue.length === 0) return;
  if (flushing) {
    scheduleFlush(token);
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
    }, token);
    connectionEpochRuntime.assertCurrent(token);
  } catch (error) {
    if (isStaleEpoch(error, token)) return;
    // Drop failed perf batches. Telemetry must not disturb the app.
  } finally {
    if (connectionEpochRuntime.isCurrent(token)) {
      flushing = false;
      if (queue.length >= batchLimit()) {
        void flushPerfEvents(token).catch(() => {});
      } else if (queue.length > 0) {
        scheduleFlush(token);
      }
    }
  }
}

export function flushPerfEventsSync(): boolean {
  if (!isPerfEnabled() || queue.length === 0) return false;
  let token: ConnectionEpochToken;
  try {
    token = captureConnectionEpoch();
  } catch {
    return false;
  }
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
        }, token).catch(() => {});
        continue;
      } catch {
        // Telemetry must not disturb app teardown.
      }
    }
    queue = batch.concat(pending, queue);
    scheduleFlush(token);
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
  configLoadToken = null;
  queue = [];
  preInitQueue = [];
  flushing = false;
}
