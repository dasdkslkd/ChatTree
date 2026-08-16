import type { ToolPermissionMode } from '../types/message';

// 每个对话一份的输入框草稿：text 为输入框当前内容，editing 非空表示正处于编辑态。
export interface ComposerEditDraft {
  targetNodeId: string;
  returnNodeId: string | null;
  toolPermissionMode: ToolPermissionMode | null;
}

export interface ComposerDraft {
  text: string;
  editing: ComposerEditDraft | null;
}

// 新对话阶段（尚无 conversationId）的输入框草稿键。
export const NEW_COMPOSER_DRAFT_KEY = 'new';

const drafts = new Map<string, ComposerDraft>();

export function getComposerDraft(key: string): ComposerDraft | undefined {
  return drafts.get(key);
}

export function setComposerDraft(key: string, draft: ComposerDraft): void {
  drafts.set(key, draft);
}

export function removeComposerDraft(key: string): void {
  drafts.delete(key);
}