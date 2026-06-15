import './App.css'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Wifi, WifiOff } from 'lucide-react'
import ChatPage from './pages/MainPage'
import { SettingsDialog } from './components/SettingsDialog'
import { useNavigationStore } from './store/navigationStore'
import { useModelStore } from './store/modelStore'
import { useEffect, useState } from 'react'

function App() {
  const { settingsOpen, settingsSection, closeSettings, openSettings } = useNavigationStore();
  const { currentProvider, currentModel, loadConfig, loadProviders } = useModelStore();
  const [connected, setConnected] = useState(true);

  useEffect(() => {
    (async () => {
      await loadConfig();
      await loadProviders();
    })();
  }, []);

  // Connectivity check
  useEffect(() => {
    const check = async () => {
      try {
        const resp = await fetch('/api/config', { method: 'GET', signal: AbortSignal.timeout(5000) });
        setConnected(resp.ok);
      } catch {
        setConnected(false);
      }
    };
    check();
    const interval = setInterval(check, 30000);
    return () => clearInterval(interval);
  }, []);

  const getModelDisplay = (): string => {
    if (currentProvider && currentModel) return `${currentProvider} / ${currentModel}`;
    return '未选择模型';
  };

  return (
    <TooltipProvider>
      <div className="flex flex-col h-screen w-screen" style={{ background: 'var(--bg-surface-under)' }}>
        {/* Main surface — floating card */}
        <div
          className="flex-1 flex flex-col min-h-0 overflow-hidden"
          style={{
            background: 'var(--bg-surface)',
            borderTopLeftRadius: '16px',
            borderBottomLeftRadius: '16px',
            boxShadow: 'var(--shadow-md), var(--highlight-top)',
          }}
        >
          {/* Page content */}
          <div className="flex-1 overflow-hidden">
            <ChatPage />
          </div>

          {/* Status bar — inside main surface, below content */}
          <footer
            className="flex-shrink-0 flex items-center gap-1.5 px-3.5 select-none"
            style={{
              height: 'var(--height-statusbar)',
              background: 'var(--bg-surface-under)',
              fontSize: '11.5px',
              color: 'var(--fg-tertiary)',
              borderTop: '0.5px solid var(--border)',
            }}
          >
            {/* Connection status */}
            <div className="flex items-center gap-1.5 px-1.5 py-0.5 rounded" style={{ borderRadius: '6px' }}>
              {connected ? (
                <Wifi className="w-3 h-3" style={{ color: 'var(--accent-green)' }} />
              ) : (
                <WifiOff className="w-3 h-3" style={{ color: 'var(--accent-red)' }} />
              )}
              <span>{connected ? '已连接' : '未连接'}</span>
            </div>

            <div className="flex-1" />

            <div className="w-px h-3" style={{ background: 'var(--border)' }} />

            {/* Current model — click to open settings */}
            <div
              className="flex items-center gap-1.5 px-1.5 py-0.5 rounded cursor-pointer transition-opacity"
              style={{ borderRadius: '6px', fontFamily: 'var(--font-mono)' }}
              onClick={() => openSettings('providers')}
              title="点击打开设置"
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
            >
              <span>{getModelDisplay()}</span>
            </div>
          </footer>
        </div>

        {/* Settings dialog */}
        <SettingsDialog
          open={settingsOpen}
          onOpenChange={(open) => { if (!open) closeSettings(); }}
          defaultSection={settingsSection}
        />
      </div>
      <Toaster />
    </TooltipProvider>
  );
}

export default App
