import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type ThemePreference = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'chattree.theme';

const THEME_TRANSITION_CLASS = 'theme-switching';
const THEME_TRANSITION_MS = 320;

function getSystemTheme(): ResolvedTheme {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'dark';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  return preference === 'system' ? getSystemTheme() : preference;
}

function applyResolvedTheme(resolved: ResolvedTheme, animate: boolean): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  if (animate && typeof window !== 'undefined') {
    root.classList.add(THEME_TRANSITION_CLASS);
    window.setTimeout(() => root.classList.remove(THEME_TRANSITION_CLASS), THEME_TRANSITION_MS);
  }
  root.classList.toggle('dark', resolved === 'dark');
  root.classList.toggle('light', resolved === 'light');
}

interface ThemeState {
  theme: ThemePreference;
  resolvedTheme: ResolvedTheme;
  setTheme: (theme: ThemePreference) => void;
}

export const useThemeStore = create<ThemeState>()(
  persist(
    (set, get) => ({
      theme: 'system',
      resolvedTheme: getSystemTheme(),
      setTheme: (theme) => {
        const resolvedTheme = resolveTheme(theme);
        applyResolvedTheme(resolvedTheme, resolvedTheme !== get().resolvedTheme);
        set({ theme, resolvedTheme });
      },
    }),
    {
      name: THEME_STORAGE_KEY,
      partialize: (state) => ({ theme: state.theme }),
    },
  ),
);

// 注水：persist 同步读取 localStorage 后立即应用，与 index.html 内联脚本衔接，杜绝闪烁
const initialResolvedTheme = resolveTheme(useThemeStore.getState().theme);
applyResolvedTheme(initialResolvedTheme, false);
useThemeStore.setState({ resolvedTheme: initialResolvedTheme });

// 跟随系统主题变化（仅当偏好为 system 时生效）
if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (useThemeStore.getState().theme !== 'system') return;
    const resolvedTheme = getSystemTheme();
    applyResolvedTheme(resolvedTheme, true);
    useThemeStore.setState({ resolvedTheme });
  });
}
