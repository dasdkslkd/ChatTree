import { Monitor, Moon, Palette, Sun } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Switch } from '@/components/ui/switch';
import { useThemeStore, type ThemePreference } from '@/store/themeStore';
import { useUiPreferencesStore } from '@/store/uiPreferencesStore';

const THEME_OPTIONS: Array<{
  key: ThemePreference;
  label: string;
  icon: typeof Sun;
  previewClass: string;
}> = [
  { key: 'system', label: '跟随系统', icon: Monitor, previewClass: 'theme-preview-system' },
  { key: 'light', label: '亮色', icon: Sun, previewClass: 'theme-preview-light' },
  { key: 'dark', label: '暗色', icon: Moon, previewClass: 'theme-preview-dark' },
];

export function AppearanceSection() {
  const theme = useThemeStore((state) => state.theme);
  const setTheme = useThemeStore((state) => state.setTheme);
  const compactMode = useUiPreferencesStore((state) => state.compactMode);
  const setCompactMode = useUiPreferencesStore((state) => state.setCompactMode);
  const streamCursor = useUiPreferencesStore((state) => state.streamCursor);
  const setStreamCursor = useUiPreferencesStore((state) => state.setStreamCursor);

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--fg-85)' }}>
          <Palette className="h-5 w-5" />
          外观
        </h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
          主题保存在本地，立即生效并伴随 300ms 平滑过渡；「跟随系统」随系统的亮暗设置自动切换
        </p>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6">
        <div className="max-w-[760px] space-y-4">
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="px-4 pt-3">
              <span
                className="block mb-3"
                style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.06em', color: 'var(--fg-tertiary)' }}
              >
                主题
              </span>
            </div>
            <div className="px-4 pb-4">
              <div className="theme-cards">
                {THEME_OPTIONS.map((option) => {
                  const Icon = option.icon;
                  const isActive = theme === option.key;
                  return (
                    <button
                      key={option.key}
                      type="button"
                      className={cn('theme-card', isActive && 'is-active')}
                      onClick={() => setTheme(option.key)}
                      aria-pressed={isActive}
                    >
                      <span className={cn('theme-card-preview', option.previewClass)}>
                        <span className="theme-preview-side" />
                        <span className="theme-preview-body" />
                      </span>
                      <span className="theme-card-name">
                        <Icon className="h-3.5 w-3.5" />
                        {option.label}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="appearance-row">
              <span className="appearance-row-label">
                界面字号
                <span className="appearance-row-desc">消息区正文字号</span>
              </span>
              <span className="appearance-row-value">14.5px</span>
            </div>
            <div className="appearance-row">
              <span className="appearance-row-label">
                紧凑模式
                <span className="appearance-row-desc">减小消息间距与内边距</span>
              </span>
              <Switch checked={compactMode} onCheckedChange={setCompactMode} aria-label="紧凑模式" />
            </div>
            <div className="appearance-row">
              <span className="appearance-row-label">
                流式光标
                <span className="appearance-row-desc">输出时显示闪烁光标，完成后渐隐</span>
              </span>
              <Switch checked={streamCursor} onCheckedChange={setStreamCursor} aria-label="流式光标" />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
