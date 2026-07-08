export const ACTIVE_STREAM_VISIBLE_POLL_MS = 5000;
export const ACTIVE_STREAM_IDLE_POLL_MS = 30000;
export const CONVERSATION_ACTIVE_STREAM_IDLE_LOOKUPS = 3;
export const CONVERSATION_ACTIVE_STREAM_HINTED_LOOKUPS = 10;
export const TASK_NOTIFICATION_DELIVERY_POLL_MS = 200;
export const TASK_NOTIFICATION_DELIVERY_LOOKUPS = 12;

export function getActiveStreamPollingDelay(options: {
  activeStreamCount: number;
  documentHidden: boolean;
}): number | null {
  if (options.documentHidden) return null;
  return options.activeStreamCount > 0
    ? ACTIVE_STREAM_VISIBLE_POLL_MS
    : ACTIVE_STREAM_IDLE_POLL_MS;
}

export function getConversationActiveStreamLookupLimit(options: {
  activeStreamHintCount: number;
}): number {
  return options.activeStreamHintCount > 0
    ? CONVERSATION_ACTIVE_STREAM_HINTED_LOOKUPS
    : CONVERSATION_ACTIVE_STREAM_IDLE_LOOKUPS;
}

export function shouldProbeBackendScheduledFollowup(options: {
  finishStatus: 'completed' | 'error' | 'stopped';
  hasQueuedFollowup: boolean;
}): boolean {
  return options.finishStatus === 'completed' && !options.hasQueuedFollowup;
}

export function shouldProbeTaskNotificationDelivery(options: {
  finishStatus: 'completed' | 'error' | 'stopped';
}): boolean {
  return options.finishStatus === 'completed'
    || options.finishStatus === 'error'
    || options.finishStatus === 'stopped';
}
