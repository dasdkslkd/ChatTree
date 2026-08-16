import '../App.css'
import { Toaster } from '@/components/ui/sonner'
import { TextTooltip } from '@/components/ui/text-tooltip'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Share2, Wifi, WifiOff, Loader2, AlertCircle } from 'lucide-react'
import { SettingsPageView } from '../components/SettingsDialog'
import { useNavigationStore } from '../store/navigationStore'
import { useModelStore } from '../store/modelStore'
import { useConversationStore } from '../store/conversationStore'
import { flushPerfEventsSync, loadPerfConfig } from '../perf/client'
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import type { NodeUsage, UsageInfo } from '../types/message'
import type { BoundServerContext } from './connectionIdentity'
import { installPageLifecycleFlush } from './pageLifecycle'
import {
  initializeServerSessionStores,
  ServerSessionInitializationOwner,
} from './serverSessionInitialization'

const ChatPage = lazy(() => import('../pages/MainPage'));

function formatTokens(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}K`;
  return String(value);
}

export default function ServerSessionApp({
  binding,
  connected,
}: {
  binding: BoundServerContext;
  connected: boolean;
}) {
  void binding;
  const { activePage, settingsSection, openSettings } = useNavigationStore();
  const { currentProvider, currentModel, loadMetadata, getMetadata, config } = useModelStore();
  const { currentConversation, treeData, currentNodeId, loadTree } = useConversationStore();
  const initializationOwnerRef = useRef<ServerSessionInitializationOwner | null>(null);
  const [contextHovered, setContextHovered] = useState(false);
  const [treeError, setTreeError] = useState<string | null>(null);
  const currentConversationId = currentConversation?.id ?? null;

  useEffect(() => {
    const owner = new ServerSessionInitializationOwner({
      initialize: () => initializeServerSessionStores({
        loadConfig: () => useModelStore.getState().loadConfig(),
        getConfig: () => useModelStore.getState().config,
        getError: () => useModelStore.getState().error,
      }),
      scheduler: window,
    });
    initializationOwnerRef.current = owner;
    owner.start();
    void loadPerfConfig();
    const disposePageLifecycle = installPageLifecycleFlush(
      window,
      document,
      flushPerfEventsSync,
    );

    return () => {
      initializationOwnerRef.current = null;
      owner.dispose();
      disposePageLifecycle();
    };
  }, []);

  useEffect(() => {
    initializationOwnerRef.current?.setConnected(connected);
  }, [connected]);

  useEffect(() => {
    if (connected && currentProvider) {
      void loadMetadata(currentProvider);
    }
  }, [connected, currentProvider, loadMetadata]);

  useEffect(() => {
    if (!currentConversationId) return;
    let cancelled = false;
    loadTree(currentConversationId)
      .then(() => {
        if (!cancelled) setTreeError(null);
      })
      .catch((err) => {
        if (!cancelled) setTreeError(err instanceof Error ? err.message : '上下文用量加载失败');
      });
    return () => { cancelled = true; };
  }, [currentConversationId, loadTree]);

  const isCurrentProxy = currentProvider ? config?.provider?.[currentProvider]?.source === 'reverse_proxy' : false;
  const getModelDisplay = (): string => {
    if (currentProvider && currentModel) {
      return isCurrentProxy ? currentModel : `${currentProvider} / ${currentModel}`;
    }
    return '未选择模型';
  };

  const metadataContextLimit = getMetadata(currentProvider, currentModel)?.context_length ?? null;
  const tipNodeId = currentNodeId || currentConversation?.current_node_id || null;
  const tipNode = useMemo(
    () => (treeData && tipNodeId ? treeData.nodes.find((n) => n.id === tipNodeId) ?? null : null),
    [treeData, tipNodeId],
  );
  const tipNodeUsage: NodeUsage | null = tipNode?.usage ?? null;
  const contextUsage = useMemo<{ used: number; usage: UsageInfo | null }>(() => {
    const active = tipNodeUsage?.active_context_usage ?? null;
    return {
      used: active?.total_tokens ?? 0,
      usage: active,
    };
  }, [tipNodeUsage]);
  const configuredContextLimit = config?.context_window ?? null;
  const contextLimit = configuredContextLimit === null
    ? metadataContextLimit
    : Math.min(configuredContextLimit, metadataContextLimit ?? configuredContextLimit);
  const contextLoading = Boolean(currentConversation) && !treeData && !treeError;
  const contextUsed = contextUsage.used;
  const contextPercent = contextLimit ? Math.min(100, Math.max(0, (contextUsed / contextLimit) * 100)) : 0;
  const contextBarColor = contextPercent >= 90
    ? 'var(--accent-red)'
    : contextPercent >= 80
      ? 'color-mix(in srgb, var(--accent-green) 45%, transparent)'
      : 'var(--accent-green)';
  const contextTitle = treeError
    ? `上下文用量加载失败：${treeError}`
    : `上下文用量：${formatTokens(contextUsed)} / ${formatTokens(contextLimit)}${contextLimit ? ` (${contextPercent.toFixed(1)}%)` : ''}`;
  const contextFree = Math.max(0, (contextLimit ?? 0) - contextUsed);
  const contextSegments = useMemo(() => {
    const usage = contextUsage.usage;
    const cache = (usage?.cached_tokens ?? 0) + (usage?.cache_read_input_tokens ?? 0) + (usage?.cache_creation_input_tokens ?? 0);
    const segments = [
      { key: 'input', label: '输入', tokens: Math.max(0, usage?.input_tokens ?? 0), color: 'var(--accent-green)' },
      { key: 'output', label: '输出', tokens: Math.max(0, usage?.output_tokens ?? 0), color: 'var(--accent-green)' },
      { key: 'reasoning', label: '推理', tokens: Math.max(0, usage?.reasoning_tokens ?? 0), color: 'color-mix(in srgb, var(--accent-green) 45%, transparent)' },
      { key: 'cache', label: '缓存', tokens: Math.max(0, cache), color: 'var(--fg-tertiary)' },
    ].filter((segment) => segment.tokens > 0);

    if (segments.length === 0 && contextUsed > 0) {
      return [{ key: 'used', label: '已用', tokens: contextUsed, color: contextBarColor }];
    }
    return segments;
  }, [contextBarColor, contextUsage.usage, contextUsed]);
  const contextLevel = contextPercent >= 90 ? 'danger' : contextPercent >= 80 ? 'warn' : 'ok';
  const cacheHitRate = useMemo(() => {
    const usage = contextUsage.usage;
    const hit = (usage?.cached_tokens ?? 0) + (usage?.cache_read_input_tokens ?? 0);
    const inputContext = (usage?.input_tokens ?? 0)
      + (usage?.cache_creation_input_tokens ?? 0)
      + (usage?.cache_read_input_tokens ?? 0);
    if (inputContext <= 0 || hit <= 0) return null;
    return hit / inputContext;
  }, [contextUsage.usage]);
  const contextFlag = contextLevel === 'danger' ? '临界' : contextLevel === 'warn' ? '偏高' : '健康';
  const contextFlagStyle = contextLevel === 'danger'
    ? { color: 'var(--accent-red)', background: 'color-mix(in srgb, var(--accent-red) 14%, transparent)' }
    : contextLevel === 'warn'
      ? { color: 'color-mix(in srgb, var(--accent-green) 45%, transparent)', background: 'color-mix(in srgb, var(--accent-green) 14%, transparent)' }
      : { color: 'var(--accent-green)', background: 'color-mix(in srgb, var(--accent-green) 14%, transparent)' };

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
            <>
              <Suspense fallback={null}>
                <div className="h-full" style={{ display: activePage === 'settings' ? 'none' : 'block' }}>
                  <ChatPage />
                </div>
              </Suspense>
              <div className="h-full" style={{ display: activePage === 'settings' ? 'block' : 'none' }}>
                <SettingsPageView defaultSection={settingsSection} />
              </div>
            </>
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
              className="hover-row flex items-center"
              style={{
                gap: '5px',
                padding: '2px 7px',
                borderRadius: 'var(--radius-sm)',
                whiteSpace: 'nowrap',
                position: 'relative',
              }}
              aria-label={contextTitle}
              onMouseEnter={() => setContextHovered(true)}
              onMouseLeave={() => setContextHovered(false)}
            >
              {contextLoading ? (
                <Loader2 className="w-3 h-3 animate-spin" style={{ color: 'var(--fg-tertiary)' }} />
              ) : treeError ? (
                <AlertCircle className="w-3 h-3" style={{ color: 'var(--accent-red)' }} />
              ) : (
                <>
                  <span style={{ fontVariantNumeric: 'tabular-nums' }}>
                    {formatTokens(contextUsed)} / {formatTokens(contextLimit)}
                  </span>
                  <div
                    aria-hidden="true"
                    style={{
                      width: '54px',
                      height: '4px',
                      borderRadius: '9999px',
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
                </>
              )}
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
                    {!contextLoading && !treeError && (
                      <span
                        style={{
                          marginLeft: 'auto',
                          padding: '1px 7px',
                          borderRadius: '9999px',
                          fontSize: '10px',
                          fontWeight: 500,
                          ...contextFlagStyle,
                        }}
                      >
                        {contextFlag}
                      </span>
                    )}
                  </div>

                  {contextLoading ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 0', color: 'var(--fg-tertiary)', fontSize: 'var(--text-xs)' }}>
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      <span>正在加载上下文用量…</span>
                    </div>
                  ) : treeError ? (
                    <div style={{ padding: '6px 0', color: 'var(--accent-red)', fontSize: 'var(--text-xs)', lineHeight: 1.5 }}>
                      {treeError}
                    </div>
                  ) : (
                    <>
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
                        {cacheHitRate !== null && (
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
                                borderRadius: '2px',
                                background: 'var(--fg-tertiary)',
                              }}
                            />
                            <span style={{ color: 'var(--fg-secondary)' }}>缓存命中率</span>
                            <span style={{ color: 'var(--fg-tertiary)' }}>
                              {(cacheHitRate * 100).toFixed(1)}%
                            </span>
                          </div>
                        )}
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>

            <div className="w-px h-3" style={{ background: 'var(--border)' }} />

            {/* Current model — click to open settings */}
            <TextTooltip content="点击打开设置">
              <div
                className="hover-row flex items-center gap-1.5 px-1.5 py-0.5 rounded cursor-pointer"
                style={{ borderRadius: '6px' }}
                onClick={() => openSettings('providers')}
              >
                {isCurrentProxy && <Share2 className="h-3.5 w-3.5" style={{ color: 'var(--icon-accent)' }} />}
                <span style={isCurrentProxy ? { color: 'var(--icon-accent)' } : undefined}>{getModelDisplay()}</span>
                {isCurrentProxy && (
                  <span className="text-xs px-1 py-0 rounded-full" style={{ background: 'color-mix(in srgb, var(--icon-accent) 15%, transparent)', color: 'var(--icon-accent)' }}>代理</span>
                )}
              </div>
            </TextTooltip>
          </footer>
        </div>

      </div>
      <Toaster />
    </TooltipProvider>
  );
}
