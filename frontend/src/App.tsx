import './App.css'
import { Toaster } from '@/components/ui/sonner'
import { TextTooltip } from '@/components/ui/text-tooltip'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Wifi, WifiOff } from 'lucide-react'
import { SettingsPageView } from './components/SettingsDialog'
import { useNavigationStore } from './store/navigationStore'
import { useModelStore } from './store/modelStore'
import { useConversationStore } from './store/conversationStore'
import { lazy, Suspense, useEffect, useMemo, useState } from 'react'

const ChatPage = lazy(() => import('./pages/MainPage'));

type UsageInfo = {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  cached_tokens?: number;
  reasoning_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
};

type MessageWithUsage = {
  role?: string;
  tokens_used?: number;
  branch_total_tokens?: number;
  branch_usage_info?: UsageInfo | null;
  context_usage?: {
    turn_usage?: UsageInfo | null;
    branch_usage?: UsageInfo | null;
    active_context_usage?: UsageInfo | null;
    model_context_window?: number | null;
  } | null;
  generation_info?: {
    tokens_used?: number;
    usage_info?: UsageInfo | null;
  } | null;
};

function usageTotal(usage?: UsageInfo | null): number {
  return usage?.total_tokens ?? ((usage?.input_tokens ?? 0) + (usage?.output_tokens ?? 0));
}

function formatTokens(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}K`;
  return String(value);
}

function App() {
  const { activePage, settingsSection, openSettings } = useNavigationStore();
  const { currentProvider, currentModel, loadConfig, loadProviders, loadMetadata, getMetadata } = useModelStore();
  const { messages } = useConversationStore();
  const [connected, setConnected] = useState(true);
  const [contextHovered, setContextHovered] = useState(false);

  useEffect(() => {
    (async () => {
      await loadConfig();
      await loadProviders();
    })();
  }, []);

  useEffect(() => {
    if (currentProvider) {
      void loadMetadata(currentProvider);
    }
  }, [currentProvider, loadMetadata]);

  // Connectivity check — lightweight health endpoint, not the full config
  useEffect(() => {
    const check = async () => {
      try {
        const resp = await fetch('/api/health', { method: 'GET', signal: AbortSignal.timeout(5000) });
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

  const metadataContextLimit = getMetadata(currentProvider, currentModel)?.context_length ?? null;
  const contextUsage = useMemo(() => {
    const typedMessages = messages as MessageWithUsage[];

    for (let i = typedMessages.length - 1; i >= 0; i -= 1) {
      const message = typedMessages[i];
      const activeUsage = message.context_usage?.active_context_usage ?? message.context_usage?.branch_usage ?? null;
      const activeTotal = usageTotal(activeUsage);
      if (activeTotal > 0) {
        return {
          used: activeTotal,
          usage: activeUsage,
          contextWindow: message.context_usage?.model_context_window ?? null,
        };
      }
      const branchTotal = usageTotal(message.branch_usage_info);
      if (branchTotal > 0) return { used: branchTotal, usage: message.branch_usage_info ?? null, contextWindow: null };
      if ((message.branch_total_tokens ?? 0) > 0) {
        return { used: message.branch_total_tokens ?? 0, usage: message.branch_usage_info ?? null, contextWindow: null };
      }
    }

    const used = typedMessages
      .filter((message) => message.role === 'assistant')
      .reduce((sum, message) => {
        return sum + usageTotal(message.generation_info?.usage_info) + (message.generation_info?.usage_info ? 0 : (message.generation_info?.tokens_used ?? message.tokens_used ?? 0));
      }, 0);

    return { used, usage: null as UsageInfo | null, contextWindow: null as number | null };
  }, [messages]);
  const contextLimit = metadataContextLimit ?? contextUsage.contextWindow ?? null;
  const contextUsed = contextUsage.used;
  const contextPercent = contextLimit ? Math.min(100, Math.max(0, (contextUsed / contextLimit) * 100)) : 0;
  const contextBarColor = contextPercent >= 90
    ? 'var(--accent-red)'
    : contextPercent >= 80
      ? 'var(--accent-yellow, #d9a441)'
      : 'var(--accent-blue)';
  const contextTitle = `上下文用量：${formatTokens(contextUsed)} / ${formatTokens(contextLimit)}${contextLimit ? ` (${contextPercent.toFixed(1)}%)` : ''}`;
  const contextFree = Math.max(0, (contextLimit ?? 0) - contextUsed);
  const contextSegments = useMemo(() => {
    const usage = contextUsage.usage;
    const cache = (usage?.cached_tokens ?? 0) + (usage?.cache_read_input_tokens ?? 0) + (usage?.cache_creation_input_tokens ?? 0);
    const segments = [
      { key: 'input', label: '输入', tokens: Math.max(0, usage?.input_tokens ?? 0), color: 'var(--accent-green)' },
      { key: 'output', label: '输出', tokens: Math.max(0, usage?.output_tokens ?? 0), color: 'var(--accent-blue)' },
      { key: 'reasoning', label: '推理', tokens: Math.max(0, usage?.reasoning_tokens ?? 0), color: 'var(--accent-yellow)' },
      { key: 'cache', label: '缓存', tokens: Math.max(0, cache), color: 'var(--fg-tertiary)' },
    ].filter((segment) => segment.tokens > 0);

    if (segments.length === 0 && contextUsed > 0) {
      return [{ key: 'used', label: '已用', tokens: contextUsed, color: contextBarColor }];
    }
    return segments;
  }, [contextBarColor, contextUsage.usage, contextUsed]);
  const contextLevel = contextPercent >= 90 ? 'danger' : contextPercent >= 80 ? 'warn' : 'ok';
  const contextFlag = contextLevel === 'danger' ? '临界' : contextLevel === 'warn' ? '偏高' : '健康';
  const contextFlagStyle = contextLevel === 'danger'
    ? { color: 'var(--accent-red)', background: 'rgba(255,103,100,0.14)' }
    : contextLevel === 'warn'
      ? { color: 'var(--accent-yellow)', background: 'rgba(255,210,64,0.14)' }
      : { color: 'var(--accent-green)', background: 'rgba(64,201,119,0.14)' };

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
            <Suspense fallback={null}>
              <div className="h-full" style={{ display: activePage === 'settings' ? 'none' : 'block' }}>
                <ChatPage />
              </div>
            </Suspense>
            <div className="h-full" style={{ display: activePage === 'settings' ? 'block' : 'none' }}>
              <SettingsPageView defaultSection={settingsSection} />
            </div>
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

            <div
              className="flex items-center"
              style={{
                gap: '5px',
                padding: '2px 7px',
                borderRadius: 'var(--radius-sm)',
                whiteSpace: 'nowrap',
                position: 'relative',
                transition: 'background var(--transition-basic), color var(--transition-basic)',
              }}
              aria-label={contextTitle}
              onMouseEnter={(e) => {
                setContextHovered(true);
                e.currentTarget.style.background = 'var(--bg-button-tertiary-hover)';
                e.currentTarget.style.color = 'var(--fg-secondary)';
              }}
              onMouseLeave={(e) => {
                setContextHovered(false);
                e.currentTarget.style.background = '';
                e.currentTarget.style.color = '';
              }}
            >
              <span style={{ fontVariantNumeric: 'tabular-nums' }}>
                {formatTokens(contextUsed)} / {formatTokens(contextLimit)}
              </span>
              <div
                aria-hidden="true"
                style={{
                  width: '54px',
                  height: '4px',
                  borderRadius: 'var(--radius-full)',
                  background: 'var(--bg-button-secondary)',
                  overflow: 'hidden',
                }}
              >
                <i
                  style={{
                    display: 'block',
                    width: contextUsed > 0 ? `max(2px, ${contextPercent}%)` : '0%',
                    height: '100%',
                    borderRadius: 'inherit',
                    background: contextBarColor,
                    transition: 'width var(--transition-relaxed), background var(--transition-basic)',
                  }}
                />
              </div>
              {contextHovered && (
                <div
                  style={{
                    position: 'absolute',
                    right: 0,
                    bottom: 'calc(100% + 8px)',
                    zIndex: 90,
                    width: '300px',
                    padding: '14px',
                    border: '0.5px solid var(--border)',
                    borderRadius: 'var(--radius-xl)',
                    background: 'var(--bg-elevated)',
                    boxShadow: 'var(--shadow-2xl)',
                    color: 'var(--fg-secondary)',
                    cursor: 'default',
                    fontFamily: 'var(--font-sans)',
                  }}
                >
                  <div
                    aria-hidden="true"
                    style={{
                      position: 'absolute',
                      left: 0,
                      right: 0,
                      bottom: '-10px',
                      height: '10px',
                    }}
                  />
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '6px',
                      marginBottom: '10px',
                      color: 'var(--fg-85)',
                      fontSize: 'var(--text-sm)',
                      fontWeight: 600,
                    }}
                  >
                    <span>上下文用量</span>
                    <span
                      style={{
                        marginLeft: 'auto',
                        padding: '1px 7px',
                        borderRadius: 'var(--radius-full)',
                        fontSize: '10px',
                        fontWeight: 500,
                        ...contextFlagStyle,
                      }}
                    >
                      {contextFlag}
                    </span>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'baseline', gap: '6px', marginBottom: '8px' }}>
                    <span
                      style={{
                        color: 'var(--fg-85)',
                        fontSize: '22px',
                        fontWeight: 600,
                        fontVariantNumeric: 'tabular-nums',
                      }}
                    >
                      {formatTokens(contextUsed)}
                    </span>
                    <span style={{ color: 'var(--fg-tertiary)', fontSize: 'var(--text-xs)' }}>
                      / {formatTokens(contextLimit)} · {contextLimit ? `${contextPercent.toFixed(1)}%` : '—'}
                    </span>
                  </div>

                  <div
                    style={{
                      display: 'flex',
                      height: '18px',
                      marginBottom: '12px',
                      overflow: 'hidden',
                      border: '0.5px solid var(--border)',
                      borderRadius: 'var(--radius-sm)',
                      background: 'var(--bg-button-secondary)',
                    }}
                  >
                    {contextSegments.map((segment) => (
                      <div
                        key={segment.key}
                        style={{
                          width: contextLimit ? `${Math.max(0.35, (segment.tokens / contextLimit) * 100)}%` : '0%',
                          height: '100%',
                          background: segment.color,
                          transition: 'width var(--transition-relaxed)',
                        }}
                      />
                    ))}
                  </div>

                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    {contextSegments.map((segment) => (
                      <div
                        key={segment.key}
                        style={{
                          display: 'grid',
                          gridTemplateColumns: '11px 1fr auto',
                          alignItems: 'center',
                          gap: '8px',
                          fontSize: 'var(--text-xs)',
                        }}
                      >
                        <span style={{ width: '9px', height: '9px', borderRadius: '2px', background: segment.color }} />
                        <span style={{ color: 'var(--fg-secondary)' }}>{segment.label}</span>
                        <span style={{ color: 'var(--fg-tertiary)' }}>
                          {formatTokens(segment.tokens)}
                          {contextLimit ? ` · ${((segment.tokens / contextLimit) * 100).toFixed(1)}%` : ''}
                        </span>
                      </div>
                    ))}
                    <div
                      style={{
                        display: 'grid',
                        gridTemplateColumns: '11px 1fr auto',
                        alignItems: 'center',
                        gap: '8px',
                        fontSize: 'var(--text-xs)',
                      }}
                    >
                      <span
                        style={{
                          width: '9px',
                          height: '9px',
                          border: '0.5px solid var(--border)',
                          borderRadius: '2px',
                          background: 'var(--bg-button-secondary)',
                        }}
                      />
                      <span style={{ color: 'var(--fg-secondary)' }}>可用空间</span>
                      <span style={{ color: 'var(--fg-tertiary)' }}>
                        {contextLimit ? formatTokens(contextFree) : '—'}
                        {contextLimit ? ` · ${((contextFree / contextLimit) * 100).toFixed(1)}%` : ''}
                      </span>
                    </div>
                  </div>
                </div>
              )}
            </div>

            <div className="w-px h-3" style={{ background: 'var(--border)' }} />

            {/* Current model — click to open settings */}
            <TextTooltip content="点击打开设置">
              <div
                className="flex items-center gap-1.5 px-1.5 py-0.5 rounded cursor-pointer transition-opacity"
                style={{ borderRadius: '6px' }}
                onClick={() => openSettings('providers')}
                onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
              >
                <span>{getModelDisplay()}</span>
              </div>
            </TextTooltip>
          </footer>
        </div>

      </div>
      <Toaster />
    </TooltipProvider>
  );
}

export default App
