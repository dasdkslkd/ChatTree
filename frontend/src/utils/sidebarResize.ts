import {
  LEFT_SIDEBAR_STORAGE_KEY,
  RIGHT_PANEL_STORAGE_KEY,
} from '../runtime/profileStorage';

export type SidebarResizeSide = 'left' | 'right';

export type SidebarWidthConfig = {
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
};

export const SIDEBAR_KEYBOARD_STEP = 16;

export const LEFT_SIDEBAR_WIDTH_STORAGE_KEY = LEFT_SIDEBAR_STORAGE_KEY;
export const RIGHT_PANEL_WIDTH_STORAGE_KEY = RIGHT_PANEL_STORAGE_KEY;

export const LEFT_SIDEBAR_WIDTH: SidebarWidthConfig = {
  defaultWidth: 224,
  minWidth: 220,
  maxWidth: 520,
};

export const RIGHT_PANEL_WIDTH: SidebarWidthConfig = {
  defaultWidth: 280,
  minWidth: 240,
  maxWidth: 680,
};

type SidebarStorage = Pick<Storage, 'getItem' | 'setItem'>;

export function clampSidebarWidth(width: number, config: SidebarWidthConfig): number {
  return Math.min(config.maxWidth, Math.max(config.minWidth, Math.round(width)));
}

export function getPointerResizedSidebarWidth(
  side: SidebarResizeSide,
  startWidth: number,
  startClientX: number,
  currentClientX: number,
  config: SidebarWidthConfig,
): number {
  const delta = side === 'left' ? currentClientX - startClientX : startClientX - currentClientX;
  return clampSidebarWidth(startWidth + delta, config);
}

export function getKeyboardResizedSidebarWidth(
  side: SidebarResizeSide,
  key: string,
  currentWidth: number,
  config: SidebarWidthConfig,
): number {
  if (key === 'Home') return config.minWidth;
  if (key === 'End') return config.maxWidth;

  if (key !== 'ArrowLeft' && key !== 'ArrowRight') {
    return currentWidth;
  }

  const direction = side === 'left'
    ? (key === 'ArrowRight' ? 1 : -1)
    : (key === 'ArrowLeft' ? 1 : -1);

  return clampSidebarWidth(currentWidth + (direction * SIDEBAR_KEYBOARD_STEP), config);
}

export function readStoredSidebarWidth(
  storage: SidebarStorage | null | undefined,
  key: string,
  config: SidebarWidthConfig,
): number {
  if (!storage) return config.defaultWidth;

  try {
    const raw = storage.getItem(key);
    if (!raw) return config.defaultWidth;

    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      return config.defaultWidth;
    }

    return clampSidebarWidth(parsed, config);
  } catch {
    return config.defaultWidth;
  }
}

export function writeStoredSidebarWidth(
  storage: SidebarStorage | null | undefined,
  key: string,
  width: number,
  config: SidebarWidthConfig,
): number {
  const clamped = clampSidebarWidth(width, config);

  try {
    storage?.setItem(key, String(clamped));
  } catch {
    // Storage can fail in private mode or when quota is exhausted; keep UI state usable.
  }

  return clamped;
}
