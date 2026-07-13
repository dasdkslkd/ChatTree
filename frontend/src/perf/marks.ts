import { recordFrontendEvent } from './client';

export function perfNow(): number {
  if (typeof performance !== 'undefined' && typeof performance.now === 'function') {
    return performance.now();
  }
  return Date.now();
}

export function recordMark(name: string, attrs: Record<string, unknown> = {}): void {
  recordFrontendEvent({ type: 'mark', name, attrs });
}

export function recordSpan(
  name: string,
  startedAt: number,
  attrs: Record<string, unknown> = {},
): void {
  recordFrontendEvent({
    type: 'span',
    name,
    duration_ms: Math.max(0, perfNow() - startedAt),
    attrs,
  });
}
