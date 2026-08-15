import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export const UI_PREFERENCES_STORAGE_KEY = 'chattree.ui';

const COMPACT_CLASS = 'compact-ui';

function applyCompactMode(enabled: boolean): void {
  if (typeof document === 'undefined') return;
  document.documentElement.classList.toggle(COMPACT_CLASS, enabled);
}

interface UiPreferencesState {
  compactMode: boolean;
  streamCursor: boolean;
  setCompactMode: (value: boolean) => void;
  setStreamCursor: (value: boolean) => void;
}

export const useUiPreferencesStore = create<UiPreferencesState>()(
  persist(
    (set) => ({
      compactMode: false,
      streamCursor: true,
      setCompactMode: (compactMode) => {
        applyCompactMode(compactMode);
        set({ compactMode });
      },
      setStreamCursor: (streamCursor) => set({ streamCursor }),
    }),
    { name: UI_PREFERENCES_STORAGE_KEY },
  ),
);

// 注水：persist 同步读取 localStorage 后立即应用
applyCompactMode(useUiPreferencesStore.getState().compactMode);
