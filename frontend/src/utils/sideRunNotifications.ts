import { isSideRunKind } from './sideRunSync';

export interface SideRunNotification {
  runId: string;
  kind: string;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function parseToolPayload(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string' || !value.trim()) return asRecord(value);
  try {
    return asRecord(JSON.parse(value));
  } catch (_) {
    return null;
  }
}

function getToolResultNotification(tool: unknown): SideRunNotification | null {
  const record = asRecord(tool);
  if (!record) return null;
  const payload = parseToolPayload(record.content) ?? parseToolPayload(record.raw_content);
  if (!payload) return null;
  const runId = typeof payload.run_id === 'string' ? payload.run_id : '';
  const kind = typeof payload.kind === 'string' ? payload.kind : '';
  if (!runId || !isSideRunKind(kind)) return null;
  return { runId, kind };
}

function getSideRunNotification(value: unknown): SideRunNotification | null {
  const record = asRecord(value);
  if (!record) return null;
  const runId = typeof record.runId === 'string' ? record.runId : '';
  const kind = typeof record.kind === 'string' ? record.kind : '';
  if (!runId || !isSideRunKind(kind)) return null;
  return { runId, kind };
}

export function extractSideRunNotifications(toolInteractions: unknown[]): SideRunNotification[] {
  const notifications: SideRunNotification[] = [];
  const seen = new Set<string>();
  for (const interaction of toolInteractions) {
    const record = asRecord(interaction);
    const tools = Array.isArray(record?.tools) ? record.tools : [];
    for (const tool of tools) {
      const notification = getToolResultNotification(tool);
      if (!notification || seen.has(notification.runId)) continue;
      notifications.push(notification);
      seen.add(notification.runId);
    }
  }
  return notifications;
}

export function collectSideRunNotifications(
  toolInteractions: unknown[],
  sideRunNotifications: unknown[] = [],
): SideRunNotification[] {
  const notifications: SideRunNotification[] = [];
  const seen = new Set<string>();
  for (const notification of extractSideRunNotifications(toolInteractions)) {
    if (seen.has(notification.runId)) continue;
    notifications.push(notification);
    seen.add(notification.runId);
  }
  for (const item of sideRunNotifications) {
    const notification = getSideRunNotification(item);
    if (!notification || seen.has(notification.runId)) continue;
    notifications.push(notification);
    seen.add(notification.runId);
  }
  return notifications;
}
