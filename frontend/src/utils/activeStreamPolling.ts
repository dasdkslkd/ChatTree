export const ACTIVE_STREAM_VISIBLE_POLL_MS = 5000;
export const ACTIVE_STREAM_IDLE_POLL_MS = 30000;

export function getActiveStreamPollingDelay(options: {
  activeStreamCount: number;
  documentHidden: boolean;
}): number | null {
  if (options.documentHidden) return null;
  return options.activeStreamCount > 0
    ? ACTIVE_STREAM_VISIBLE_POLL_MS
    : ACTIVE_STREAM_IDLE_POLL_MS;
}

export function shouldProbeBackendScheduledFollowup(options: {
  finishStatus: 'completed' | 'error' | 'stopped';
  hasQueuedFollowup: boolean;
}): boolean {
  return options.finishStatus === 'completed' && !options.hasQueuedFollowup;
}
