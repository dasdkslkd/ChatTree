import type { WorkspaceContext } from '../types/conversation';
import {
  MANUAL_PROJECTS_STORAGE_KEY,
  PROJECT_ORDER_STORAGE_KEY,
  profileStorageKey,
} from '../runtime/profileStorage';
import { getProfileContext } from '../runtime/profileContext';

const PROFILE_ID = getProfileContext().profileId;
const PROFILE_MANUAL_PROJECTS_STORAGE_KEY = profileStorageKey(PROFILE_ID, MANUAL_PROJECTS_STORAGE_KEY);
const PROFILE_PROJECT_ORDER_STORAGE_KEY = profileStorageKey(PROFILE_ID, PROJECT_ORDER_STORAGE_KEY);

export function getBrowserStorage(): Storage | null {
  return typeof window === 'undefined' ? null : window.localStorage;
}

export function loadManualProjectWorkspaces(): WorkspaceContext[] {
  try {
    const raw = window.localStorage.getItem(PROFILE_MANUAL_PROJECTS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is WorkspaceContext =>
      !!item && typeof item.cwd === 'string' && Array.isArray(item.workspace_roots)
    );
  } catch {
    return [];
  }
}

export function saveManualProjectWorkspaces(workspaces: WorkspaceContext[]) {
  window.localStorage.setItem(PROFILE_MANUAL_PROJECTS_STORAGE_KEY, JSON.stringify(workspaces));
}

export function loadProjectOrder(): string[] {
  try {
    const raw = window.localStorage.getItem(PROFILE_PROJECT_ORDER_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : [];
  } catch (_) {
    return [];
  }
}

export function saveProjectOrder(order: string[]) {
  window.localStorage.setItem(PROFILE_PROJECT_ORDER_STORAGE_KEY, JSON.stringify(order));
}

export function mergeManualProjectWorkspace(workspaces: WorkspaceContext[], workspace: WorkspaceContext): WorkspaceContext[] {
  const existing = workspaces.filter((item) => item.cwd !== workspace.cwd);
  return [workspace, ...existing];
}
