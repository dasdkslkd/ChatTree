import type { ToolPermissionMode } from '../types/message';
import type { ConfigData } from '../types/model';

export const TOOL_PERMISSION_MODE_OPTIONS: { value: ToolPermissionMode; label: string; description: string }[] = [
  { value: 'auto_approve', label: '自动批准', description: '除显式删除外自动执行工具' },
  { value: 'modify_only', label: '修改前询问', description: '读取自动执行，修改需确认' },
  { value: 'ask_always', label: '总是询问', description: '每次工具调用都需确认' },
  { value: 'plan', label: '计划模式', description: '仅允许只读规划工具' },
];

export type ToolPermissionDraft = {
  mode: ToolPermissionMode;
  explicit: boolean;
};

export const DEFAULT_TOOL_PERMISSION_MODE: ToolPermissionMode = 'auto_approve';

export function normalizeToolPermissionMode(value: unknown): ToolPermissionMode | undefined {
  if (value === 'auto_approve' || value === 'auto' || value === 'bypass_permissions') return 'auto_approve';
  if (value === 'modify_only' || value === 'ask_on_modify' || value === 'modify') return 'modify_only';
  if (value === 'ask_always' || value === 'ask_all' || value === 'all') return 'ask_always';
  if (value === 'plan' || value === 'plan_mode') return 'plan';
  return undefined;
}

export function getConfiguredDefaultToolPermissionMode(
  config: Pick<ConfigData, 'tools'> | null | undefined,
): ToolPermissionMode {
  return normalizeToolPermissionMode(config?.tools?.default_permission_mode) ?? DEFAULT_TOOL_PERMISSION_MODE;
}

export function createToolPermissionDraft(
  mode: ToolPermissionMode = DEFAULT_TOOL_PERMISSION_MODE,
): ToolPermissionDraft {
  return { mode, explicit: false };
}

export function selectToolPermissionMode(
  _draft: ToolPermissionDraft,
  mode: ToolPermissionMode,
): ToolPermissionDraft {
  return { mode, explicit: true };
}

export function getPendingToolPermissionMode(
  draft: ToolPermissionDraft,
): ToolPermissionMode | undefined {
  return draft.explicit ? draft.mode : undefined;
}

export function markToolPermissionModeSent(
  draft: ToolPermissionDraft,
  sentMode?: ToolPermissionMode,
): ToolPermissionDraft {
  if (sentMode && draft.explicit && draft.mode !== sentMode) return draft;
  return { mode: draft.mode, explicit: false };
}

export function syncToolPermissionDraftFromBranch(
  draft: ToolPermissionDraft,
  mode: ToolPermissionMode | undefined,
): ToolPermissionDraft {
  if (draft.explicit || !mode || draft.mode === mode) return draft;
  return { mode, explicit: false };
}
