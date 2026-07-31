import {
  PROJECT_ORDER_STORAGE_KEY,
  profileStorageKey,
} from '../runtime/profileStorage';
import { getProfileContext } from '../runtime/profileContext';

const PROFILE_ID = getProfileContext().profileId;
const PROFILE_PROJECT_ORDER_STORAGE_KEY = profileStorageKey(PROFILE_ID, PROJECT_ORDER_STORAGE_KEY);

export function getBrowserStorage(): Storage | null {
  return typeof window === 'undefined' ? null : window.localStorage;
}

export function loadProjectOrder(): string[] {
  try {
    const raw = window.localStorage.getItem(PROFILE_PROJECT_ORDER_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : [];
  } catch {
    return [];
  }
}

export function saveProjectOrder(order: string[]) {
  window.localStorage.setItem(PROFILE_PROJECT_ORDER_STORAGE_KEY, JSON.stringify(order));
}
