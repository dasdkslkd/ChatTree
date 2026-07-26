import type { ToolPermissionMode, TaskContextMode } from '../types/message';
import type { TranscriptItem } from '../types/transcript';
import { normalizeToolPermissionMode } from './toolPermissionDraft';

export function normalizeTaskContextMode(value: unknown): TaskContextMode | undefined {
  return value === 'attached' || value === 'detached' ? value : undefined;
}

export function getBranchToolPermissionMode(
  items: TranscriptItem[],
  nodeId: string | null,
): ToolPermissionMode | undefined {
  if (!nodeId) return undefined;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.type === 'user_message' && item.node_id === nodeId) {
      const mode = normalizeToolPermissionMode(item.tool_permission_mode);
      if (mode) return mode;
    }
  }
  return undefined;
}

export function getBranchTaskContextMode(
  items: TranscriptItem[],
  nodeId: string | null,
): TaskContextMode | undefined {
  if (!nodeId) return undefined;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.type === 'user_message' && item.node_id === nodeId) {
      const mode = normalizeTaskContextMode(item.task_context_mode);
      if (mode) return mode;
    }
  }
  return undefined;
}
