import type { ToolPermissionMode } from '../types/message';

export type ToolPermissionDraft = {
  mode: ToolPermissionMode;
  explicit: boolean;
};

export const DEFAULT_TOOL_PERMISSION_MODE: ToolPermissionMode = 'ask_always';

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
