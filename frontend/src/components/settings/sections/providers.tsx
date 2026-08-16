import { useEffect, useState, useCallback, useRef } from 'react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { TextTooltip } from '@/components/ui/text-tooltip';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/dialog';
import { Switch } from '@/components/ui/switch';
import { Settings, Plus, Trash2, Eye, EyeOff, Loader2, ExternalLink, RefreshCw, CheckCircle2 } from 'lucide-react';
import { toast } from '@/utils/toast';
import { configApi, type SubscriptionLoginHandle } from '@/api/config';
import { getApiErrorMessage } from '@/api/errors';
import { useModelStore } from '@/store/modelStore';
import type {
  ConfigData,
  ContextWindowLimit,
  ModelProviderConfig,
} from '@/types/model';

const AUTH_OPTIONS: {
  value: 'api_key' | 'codex' | 'copilot' | 'claude';
  label: string;
  description: string;
}[] = [
  { value: 'api_key', label: 'API Key', description: '使用 API Key 认证' },
  { value: 'codex', label: 'ChatGPT Plus/Pro', description: 'OAuth 设备码登录' },
  { value: 'copilot', label: 'GitHub Copilot', description: 'OAuth 设备码登录' },
  { value: 'claude', label: 'Claude (Anthropic)', description: '从 Claude CLI 导入' },
];

const DEFAULT_PROVIDER_CONFIG: ModelProviderConfig = {
  name: '',
  models: [],
  api_key: '',
  base_url: '',
  organization: '',
  project: '',
  hidden_models: [],
  enabled: false,
};

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9一-鿿]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

function formatResetTime(resetAt: unknown): string {
  if (!resetAt) return '—';
  const ts = typeof resetAt === 'number' ? resetAt : parseInt(String(resetAt), 10);
  if (!ts || isNaN(ts)) return String(resetAt);
  // reset_at 可能是秒或毫秒
  const ms = ts > 1e12 ? ts : ts * 1000;
  const diff = ms - Date.now();
  if (diff <= 0) return '已重置';
  const hours = Math.floor(diff / 3600000);
  const mins = Math.floor((diff % 3600000) / 60000);
  return hours > 0 ? `${hours}小时${mins}分钟后` : `${mins}分钟后`;
}

export function ProvidersSection() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [loading, setLoading] = useState(true);

  // 统一编辑/新增对话框
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editProviderId, setEditProviderId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<ModelProviderConfig>({ ...DEFAULT_PROVIDER_CONFIG });
  const [editNameInput, setEditNameInput] = useState('');
  const [editIdInput, setEditIdInput] = useState('');
  const [editNewModelInput, setEditNewModelInput] = useState('');
  const [editFetchingModels, setEditFetchingModels] = useState(false);
  const [editSaving, setEditSaving] = useState(false);

  // 删除确认
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  // 订阅登录流程
  const [loginHandle, setLoginHandle] = useState<SubscriptionLoginHandle | null>(null);
  const [loginPolling, setLoginPolling] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [enterpriseDomain, setEnterpriseDomain] = useState('');
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 额度查询
  const [quotaInfo, setQuotaInfo] = useState<Record<string, unknown> | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(false);

  // 默认模型的元数据（用于判断是否渲染思考/推理控件）
  const modelMetadata = useModelStore((s) => s.modelMetadata);

  const loadConfig = useCallback(async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      setConfig(data);
      // 同步刷新全局 modelStore，使聊天侧的模型选择器即时更新
      useModelStore.getState().loadConfig({ force: true });
    } catch {
      toast.error('加载配置失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadConfig(); }, [loadConfig]);

  // 默认提供商变更时加载其模型元数据
  useEffect(() => {
    const pid = config?.default_provider;
    if (pid) void useModelStore.getState().loadMetadata(pid);
  }, [config?.default_provider]);

  const getEnabledProviders = (): string[] => {
    if (!config) return [];
    return Object.keys(config.provider).filter(p => config.provider[p]?.enabled);
  };

  const getProviderDisplayName = (providerId: string): string => {
    return config?.provider?.[providerId]?.name || providerId;
  };

  const getVisibleProviderModels = (providerId: string): string[] => {
    const provider = config?.provider?.[providerId];
    if (!provider) return [];
    const hidden = new Set(provider.hidden_models || []);
    return (provider.models || []).filter((model) => !hidden.has(model));
  };

  const handleDefaultProviderChange = async (provider: string) => {
    if (!config) return;
    const prev = config.default_provider;
    const prevModel = config.default_model || '';
    const visibleModels = getVisibleProviderModels(provider);
    const nextModel = visibleModels.includes(prevModel) ? prevModel : (visibleModels[0] || '');
    setConfig({ ...config, default_provider: provider, default_model: nextModel });
    try {
      await configApi.update({ default_provider: provider, default_model: nextModel });
      await useModelStore.getState().loadConfig({ force: true });
      toast.success('默认提供商已更新');
    } catch {
      setConfig(c => c ? { ...c, default_provider: prev, default_model: prevModel } : c);
      toast.error('保存失败');
    }
  };

  const handleDefaultModelChange = async (model: string) => {
    if (!config) return;
    const prev = config.default_model || '';
    setConfig({ ...config, default_model: model });
    try {
      await configApi.update({ default_model: model });
      await useModelStore.getState().loadConfig({ force: true });
      toast.success('默认模型已更新');
    } catch {
      setConfig(c => c ? { ...c, default_model: prev } : c);
      toast.error('保存失败');
    }
  };

  const handleContextWindowChange = async (value: string) => {
    if (!config) return;
    const previous = config.context_window ?? null;
    const contextWindow = (value === 'max' ? null : Number(value)) as ContextWindowLimit;
    setConfig({ ...config, context_window: contextWindow });
    try {
      await configApi.update({ context_window: contextWindow });
      await useModelStore.getState().loadConfig({ force: true });
      toast.success('上下文窗口已更新');
    } catch {
      setConfig(current => current ? { ...current, context_window: previous } : current);
      toast.error('保存失败');
    }
  };

  // 默认模型的思考模式/推理强度：写入 config，供新对话继承
  const handleDefaultThinkingChange = async (enabled: boolean) => {
    if (!config) return;
    const prev = config.default_thinking_enabled ?? null;
    setConfig({ ...config, default_thinking_enabled: enabled });
    try {
      await configApi.update({ default_thinking_enabled: enabled });
      await useModelStore.getState().loadConfig({ force: true });
      toast.success('默认思考模式已更新');
    } catch {
      setConfig(c => c ? { ...c, default_thinking_enabled: prev } : c);
      toast.error('保存失败');
    }
  };

  const handleDefaultEffortChange = async (level: string) => {
    if (!config) return;
    const prev = config.default_reasoning_effort ?? null;
    const next = config.default_reasoning_effort === level ? null : level;
    setConfig({ ...config, default_reasoning_effort: next });
    try {
      await configApi.update({ default_reasoning_effort: next });
      await useModelStore.getState().loadConfig({ force: true });
      toast.success('默认推理强度已更新');
    } catch {
      setConfig(c => c ? { ...c, default_reasoning_effort: prev } : c);
      toast.error('保存失败');
    }
  };

  // ── 统一打开对话框：providerId 为 null 表示新建 ──
  const openEditDialog = (providerId: string | null) => {
    if (providerId) {
      const cfg = config?.provider?.[providerId] ?? { ...DEFAULT_PROVIDER_CONFIG, name: providerId };
      setEditProviderId(providerId);
      setEditForm({ ...DEFAULT_PROVIDER_CONFIG, ...cfg, hidden_models: [...(cfg.hidden_models || [])] });
      setEditNameInput(cfg.name || providerId);
      setEditIdInput(providerId);
    } else {
      setEditProviderId(null);
      setEditForm({ ...DEFAULT_PROVIDER_CONFIG });
      setEditNameInput('');
      setEditIdInput('');
    }
    setEditNewModelInput('');
    setQuotaInfo(null);
    setLoginError(null);
    setEnterpriseDomain('');
    setEditDialogOpen(true);
    // 打开编辑弹窗时，若该提供商配置了订阅则自动查询额度
    if (providerId) {
      const pc = config?.provider?.[providerId];
      if (pc?.auth?.subscription) {
        void handleFetchQuota(providerId);
      }
    }
  };

  const handleNameInputChange = (name: string) => {
    setEditNameInput(name);
    if (!editProviderId && (!editIdInput || editIdInput === slugify(editNameInput))) {
      setEditIdInput(slugify(name));
    }
  };

  const handleEditAddModel = () => {
    const name = editNewModelInput.trim();
    if (!name) return;
    if (!editForm.models.includes(name)) {
      setEditForm(f => ({ ...f, models: [...f.models, name] }));
    }
    setEditNewModelInput('');
  };

  const toggleModelHidden = (modelName: string) => {
    setEditForm(f => {
      const hidden = new Set(f.hidden_models || []);
      if (hidden.has(modelName)) hidden.delete(modelName);
      else hidden.add(modelName);
      return { ...f, hidden_models: [...hidden] };
    });
  };

  const handleSaveProvider = async () => {
    try {
      setEditSaving(true);
      const providerConfig = { ...editForm };

      if (editProviderId) {
        // 编辑现有
        const update = { provider_configs: { [editProviderId]: providerConfig } } as {
          provider_configs: Record<string, Partial<ModelProviderConfig>>;
          default_model?: string;
        };
        if (config?.default_provider === editProviderId) {
          const hidden = new Set(providerConfig.hidden_models || []);
          const visibleModels = (providerConfig.models || []).filter((model) => !hidden.has(model));
          if (!visibleModels.includes(config.default_model || '')) {
            update.default_model = visibleModels[0] || '';
          }
        }
        await configApi.update(update);
      } else {
        // 新建
        const id = editIdInput.trim();
        if (!id) { toast.error('请输入提供商 ID'); return; }
        if (config?.provider?.[id]) { toast.error(`ID "${id}" 已存在`); return; }
        providerConfig.name = editNameInput.trim();
        await configApi.addProvider({
          id,
          name: providerConfig.name,
          base_url: providerConfig.base_url,
          api_key: providerConfig.api_key,
          auth: providerConfig.auth,
        });
      }
      toast.success('已保存');
      setEditDialogOpen(false);
      await loadConfig();
    } catch {
      toast.error('保存失败');
    } finally {
      setEditSaving(false);
    }
  };

  const handleDeleteProvider = async (providerId: string) => {
    try {
      await configApi.deleteProvider(providerId);
      toast.success('已删除');
      setDeleteConfirmId(null);
      setEditDialogOpen(false);
      await loadConfig();
    } catch {
      toast.error('删除失败');
    }
  };

  // 新建模式下先保存 provider 到后端，返回 provider id
  const ensureProviderSaved = async (): Promise<string | null> => {
    if (editProviderId) return editProviderId;
    const id = editIdInput.trim();
    if (!id || !editNameInput.trim()) {
      toast.error('请先填写名称和 ID');
      return null;
    }
    if (config?.provider?.[id]) {
      toast.error(`ID "${id}" 已存在`);
      return null;
    }
    try {
      const providerConfig = { ...editForm };
      providerConfig.name = editNameInput.trim();
      await configApi.addProvider({
        id,
        name: providerConfig.name,
        base_url: providerConfig.base_url,
        api_key: providerConfig.api_key,
        auth: providerConfig.auth,
      });
      setEditProviderId(id);
      await loadConfig();
      return id;
    } catch {
      toast.error('保存失败');
      return null;
    }
  };

  // ── 订阅登录：codex/copilot 走 OAuth 设备码 ──
  const handleStartSubscriptionLogin = async (subscription: 'codex' | 'copilot') => {
    const pid = await ensureProviderSaved();
    if (!pid) return;
    setLoginError(null);
    setEnterpriseDomain('');
    try {
      const handle = await configApi.startSubscriptionLogin(pid, subscription);
      setLoginHandle(handle);
      setLoginPolling(true);
      window.open(handle.verification_uri, '_blank');
      pollLogin(pid, subscription, handle);
    } catch (e) {
      setLoginError(String(e) || '启动登录失败');
    }
  };

  const pollLogin = async (pid: string, subscription: string, handle: SubscriptionLoginHandle) => {
    try {
      const result = await configApi.pollSubscriptionLogin(pid, subscription, handle);
      if (result.status === 'ok') {
        setLoginPolling(false);
        setLoginHandle(null);
        toast.success('登录成功');
        await loadConfig();
        // 同步 editForm.auth（用 pid 直接查找，避免闭包中的 stale state）
        const updated = await configApi.get();
        const pc = updated.provider?.[pid];
        if (pc?.auth) {
          setEditForm(f => ({ ...f, auth: pc.auth }));
        }
        // 自动拉取模型列表
        try {
          const { models } = await configApi.refreshProviderModels(pid);
          if (models?.length) {
            setEditForm(f => ({ ...f, models }));
            toast.success(`获取到 ${models.length} 个模型`);
            await loadConfig();
          }
        } catch {
          // 模型拉取失败不阻塞
        }
        handleFetchQuota(pid);
      } else {
        pollTimerRef.current = setTimeout(() => pollLogin(pid, subscription, handle), (handle.interval || 5) * 1000);
      }
    } catch (e) {
      setLoginPolling(false);
      setLoginError(String(e) || '轮询失败');
    }
  };

  const handleCancelLogin = () => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setLoginPolling(false);
    setLoginHandle(null);
    setLoginError(null);
  };

  // ── CLI 凭据导入：claude/codex ──
  const handleImportCliCredentials = async (subscription: 'claude' | 'codex') => {
    const pid = await ensureProviderSaved();
    if (!pid) return;
    try {
      await configApi.importCliCredentials(pid, subscription);
      toast.success('凭据导入成功');
      await loadConfig();
      // 同步 editForm.auth（用 pid 直接查找，避免闭包中的 stale state）
      const updated = await configApi.get();
      const pc = updated.provider?.[pid];
      if (pc?.auth) {
        setEditForm(f => ({ ...f, auth: pc.auth }));
      }
      if (subscription === 'codex') {
        try {
          const { models } = await configApi.refreshProviderModels(pid);
          if (models?.length) {
            setEditForm(f => ({ ...f, models }));
            toast.success(`获取到 ${models.length} 个模型`);
            await loadConfig();
          }
        } catch {
          // 模型拉取失败不阻塞
        }
      }
      handleFetchQuota(pid);
    } catch (error) {
      toast.error(getApiErrorMessage(error, '导入失败'));
    }
  };

  // ── 额度查询 ──
  const handleFetchQuota = async (pid?: string) => {
    const providerId = pid || editProviderId || editIdInput.trim();
    if (!providerId) return;
    try {
      setQuotaLoading(true);
      const quota = await configApi.getProviderQuota(providerId);
      setQuotaInfo(quota);
    } catch {
      toast.error('额度查询失败');
    } finally {
      setQuotaLoading(false);
    }
  };

  // ── 强制刷新模型列表 ──
  const handleRefreshModels = async () => {
    const pid = editProviderId || editIdInput.trim();
    if (!pid) return;
    try {
      setEditFetchingModels(true);
      const { models } = await configApi.refreshProviderModels(pid);
      if (models?.length) {
        setEditForm(f => ({ ...f, models }));
        toast.success(`获取到 ${models.length} 个模型`);
      } else {
        toast.error('未获取到模型');
      }
      await loadConfig();
    } catch {
      toast.error('获取失败');
    } finally {
      setEditFetchingModels(false);
    }
  };

  // 组件卸载时清理轮询定时器
  useEffect(() => {
    return () => {
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
      }
    };
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载中...</span>
      </div>
    );
  }

  const enabledProviders = getEnabledProviders();
  const providerIds = config ? Object.keys(config.provider) : [];
  const defaultProviderModels = config?.default_provider ? getVisibleProviderModels(config.default_provider) : [];
  const defaultMeta = config?.default_provider
    ? modelMetadata[config.default_provider]?.[config.default_model || '']
    : undefined;
  const defaultEffortSpec = defaultMeta?.reasoning_effort;
  const defaultThinkingSpec = defaultMeta?.thinking;
  const effectiveDefaultThinking = config?.default_thinking_enabled ?? defaultThinkingSpec?.default_enabled ?? false;
  const isEditMode = !!editProviderId;
  const currentSubscription = editForm.auth?.subscription;
  const isSubscribed = !!currentSubscription;
  const isLoggedIn = !!editForm.auth?.access;
  const isReverseProxy = editProviderId ? config?.provider?.[editProviderId]?.source === 'reverse_proxy' : false;

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      {/* Fixed header */}
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>供应商</h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>配置 API 端点与模型</p>
      </div>

      {/* Scrollable content */}
      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6 space-y-6">

      {/* Default provider */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: '0.5px solid var(--border)' }}
      >
        <div
          className="px-4 py-2.5 text-sm font-medium"
          style={{ background: 'var(--bg-button-secondary)', borderBottom: '0.5px solid var(--border)' }}
        >
          全局设置
        </div>
        <div className="px-4 py-3 space-y-4">
          <div className="grid gap-4 md:grid-cols-3">
          <div className="space-y-2">
            <Label className="text-sm" style={{ color: 'var(--fg-85)' }}>默认提供商</Label>
            {enabledProviders.length > 0 ? (
              <Select value={config?.default_provider || ''} onValueChange={handleDefaultProviderChange}>
                <SelectTrigger>
                  <SelectValue placeholder="选择默认提供商" />
                </SelectTrigger>
                <SelectContent>
                  {enabledProviders.map((p) => (
                    <SelectItem key={p} value={p}>{getProviderDisplayName(p)}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>请先启用至少一个提供商</p>
            )}
          </div>
          <div className="space-y-2">
            <Label className="text-sm" style={{ color: 'var(--fg-85)' }}>默认模型</Label>
            {config?.default_provider && defaultProviderModels.length > 0 ? (
              <Select value={config.default_model || ''} onValueChange={handleDefaultModelChange}>
                <SelectTrigger>
                  <SelectValue placeholder="选择默认模型" />
                </SelectTrigger>
                <SelectContent>
                  {defaultProviderModels.map((model) => (
                    <SelectItem key={model} value={model}>{model}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>默认提供商暂无可见模型</p>
            )}
          </div>
          <div className="space-y-2">
            <Label className="text-sm" style={{ color: 'var(--fg-85)' }}>上下文窗口</Label>
            <Select
              value={config?.context_window ? String(config.context_window) : 'max'}
              onValueChange={handleContextWindowChange}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="200000">200K</SelectItem>
                <SelectItem value="400000">400K</SelectItem>
                <SelectItem value="600000">600K</SelectItem>
                <SelectItem value="max">模型最大值</SelectItem>
              </SelectContent>
            </Select>
          </div>
          </div>

          {/* 默认模型的思考模式与推理强度 */}
          {(defaultThinkingSpec?.toggleable || (defaultEffortSpec && defaultEffortSpec.levels?.length > 0)) && (
            <div className="flex flex-wrap items-center gap-x-8 gap-y-4 pt-1">
              {defaultThinkingSpec?.toggleable && (
                <div className="flex items-center gap-3">
                  <Label className="text-sm" style={{ color: 'var(--fg-85)' }}>默认思考模式</Label>
                  <Switch
                    checked={effectiveDefaultThinking}
                    onCheckedChange={handleDefaultThinkingChange}
                  />
                </div>
              )}
              {defaultEffortSpec && defaultEffortSpec.levels?.length > 0 && (
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm" style={{ color: 'var(--fg-85)' }}>默认推理强度</span>
                  <div className="flex flex-wrap gap-1.5">
                    {defaultEffortSpec.levels.map((level) => {
                      const active = (config?.default_reasoning_effort ?? null) === level;
                      return (
                        <button
                          key={level}
                          className="px-2.5 py-1 rounded-full text-xs cursor-pointer transition-colors"
                          style={{
                            background: active ? 'var(--accent-soft)' : 'transparent',
                            color: active ? 'var(--icon-accent)' : 'var(--fg-tertiary)',
                            border: `0.5px solid ${active ? 'var(--icon-accent)' : 'var(--border)'}`,
                          }}
                          onClick={() => handleDefaultEffortChange(level)}
                        >
                          {level}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Provider list */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: '0.5px solid var(--border)' }}
      >
        <div
          className="px-4 py-2.5 flex items-center justify-between"
          style={{ background: 'var(--bg-button-secondary)', borderBottom: '0.5px solid var(--border)' }}
        >
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>提供商</span>
            <span className="text-xs px-1.5 py-0.5 rounded-full" style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
              {enabledProviders.length}/{providerIds.length}
            </span>
          </div>
          <button
            className="btn-secondary flex items-center gap-1 text-xs px-2.5 py-1 rounded-lg cursor-pointer"
            onClick={() => openEditDialog(null)}
          >
            <Plus className="h-3.5 w-3.5" />
            添加
          </button>
        </div>
        <div className="divide-y" style={{ '--tw-divide-opacity': 1, '--tw-divide-color': 'var(--border-light, rgba(255,247,240,0.05))' } as React.CSSProperties}>
          {providerIds.length > 0 ? (() => {
            const reverseProxyIds = providerIds.filter((pid) => config!.provider[pid]?.source === 'reverse_proxy');
            const remoteIds = providerIds.filter((pid) => config!.provider[pid]?.source !== 'reverse_proxy');
            const renderRow = (pid: string) => {
              const pc = config!.provider[pid];
              const isReverseProxy = pc.source === 'reverse_proxy';
              return (
                <div
                  key={pid}
                  className="hover-row flex items-center justify-between px-4 py-2.5 cursor-pointer"
                  onClick={() => openEditDialog(pid)}
                >
                  <div className="flex items-center gap-3">
                    <Settings className="h-4 w-4" style={{ color: 'var(--icon-tertiary)' }} />
                    <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{pc.name || pid}</span>
                    {isReverseProxy ? (
                      <span className="text-xs px-1.5 py-0.5 rounded-full" style={{ background: 'color-mix(in srgb, var(--icon-accent) 15%, transparent)', color: 'var(--icon-accent)' }}>
                        本地代理
                      </span>
                    ) : null}
                    {pc.enabled && (
                      <span className="text-xs px-1.5 py-0.5 rounded-full" style={{ background: 'color-mix(in srgb, var(--accent-green) 15%, transparent)', color: 'var(--accent-green)' }}>
                        已启用
                      </span>
                    )}
                  </div>
                  <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                    {pc.models?.length || 0} 个模型
                  </span>
                </div>
              );
            };
            const renderGroup = (label: string, ids: string[]) => {
              if (ids.length === 0) return null;
              return (
                <>
                  <div
                    className="px-4 py-1.5 text-xs font-medium"
                    style={{ background: 'var(--bg-button-secondary)', color: 'var(--fg-tertiary)' }}
                  >
                    {label} ({ids.length})
                  </div>
                  {ids.map(renderRow)}
                </>
              );
            };
            return (
              <>
                {renderGroup('本地代理', reverseProxyIds)}
                {renderGroup('远程配置', remoteIds)}
              </>
            );
          })() : (
            <div className="px-4 py-8 text-center" style={{ color: 'var(--fg-tertiary)' }}>
              <p className="text-sm">暂无已配置的提供商</p>
            </div>
          )}
        </div>
      </div>
      </div>

      {/* Footer spacer */}
      <div className="flex-shrink-0 h-2" />

      {/* ── 统一编辑/新增对话框 ── */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent className="sm:max-w-[1120px] max-h-[88vh] overflow-y-auto custom-scrollbar">
          <DialogHeader>
            <DialogTitle>{isEditMode ? (editForm.name || editProviderId) : '添加提供商'}</DialogTitle>
            <DialogDescription>
              {isReverseProxy
                ? '本地代理提供商：由 launcher 通过 SSH 反向隧道注入，模型来自本地 server'
                : isEditMode ? '配置提供商参数和模型列表' : '配置一个新的 AI 模型提供商'}
            </DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-2 gap-8 py-2">
            {/* ── 左列：基本配置 ── */}
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <Switch checked={editForm.enabled} onCheckedChange={(checked) => setEditForm(f => ({ ...f, enabled: checked }))} />
                <Label>启用此提供商</Label>
              </div>
              <div className="h-px" style={{ background: 'var(--border)' }} />

              {isReverseProxy ? (
                <div className="space-y-3 p-3 rounded-lg text-xs" style={{ background: 'var(--bg-button-secondary)', border: '0.5px solid var(--border)' }}>
                  <div className="flex items-center gap-1.5" style={{ color: 'var(--icon-accent)' }}>
                    <span className="font-medium">反向代理</span>
                  </div>
                  <p style={{ color: 'var(--fg-secondary)' }}>
                    此提供商通过 SSH 反向隧道桥接本地 server 的 provider。模型列表由本地配置决定，断开 SSH 连接时自动清理。
                  </p>
                  <div className="space-y-1 pt-1" style={{ color: 'var(--fg-tertiary)' }}>
                    <div>Base URL: <span className="font-mono">{editForm.base_url || '—'}</span></div>
                    <div>模型数量: {editForm.models.length}</div>
                  </div>
                </div>
              ) : (
                <>
              <div className="space-y-2">
                <Label>显示名称</Label>
                <Input
                  value={editNameInput}
                  onChange={(e) => handleNameInputChange(e.target.value)}
                  placeholder="例如：My OpenAI"
                />
              </div>

              {!isEditMode && (
                <div className="space-y-2">
                  <Label>提供商 ID</Label>
                  <Input
                    value={editIdInput}
                    onChange={(e) => setEditIdInput(e.target.value)}
                    placeholder="自动生成"
                  />
                  <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>唯一标识，仅限英文、数字和连字符</p>
                </div>
              )}

              <div className="space-y-2">
                <Label>认证方式</Label>
                <Select
                  value={currentSubscription || 'api_key'}
                  onValueChange={(v) => {
                    const sub = v === 'api_key' ? undefined : v as 'codex' | 'copilot' | 'claude';
                    setEditForm(f => ({
                      ...f,
                      auth: sub ? { type: 'oauth', subscription: sub } : undefined,
                    }));
                    setQuotaInfo(null);
                  }}
                >
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {AUTH_OPTIONS.map(opt => (
                      <SelectItem key={opt.value} value={opt.value} textValue={opt.label}>
                        <div className="flex flex-col">
                          <span>{opt.label}</span>
                          <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>{opt.description}</span>
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {/* 订阅登录/导入 + 账号信息 */}
              {isSubscribed && (
                <div className="space-y-2 p-3 rounded-lg" style={{ background: 'var(--bg-button-secondary)', border: '0.5px solid var(--border)' }}>
                  {currentSubscription === 'copilot' && !isLoggedIn && (
                    <Input
                      value={enterpriseDomain}
                      onChange={(e) => setEnterpriseDomain(e.target.value)}
                      placeholder="企业版域名（可选，如 github.example.com）"
                    />
                  )}
                  {!isLoggedIn && (currentSubscription === 'codex' || currentSubscription === 'copilot') && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="w-full"
                      onClick={() => handleStartSubscriptionLogin(currentSubscription as 'codex' | 'copilot')}
                      disabled={loginPolling}
                    >
                      {loginPolling ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : null}
                      {loginPolling ? '等待授权...' : '登录'}
                    </Button>
                  )}
                  {!isLoggedIn && (currentSubscription === 'claude' || currentSubscription === 'codex') && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="w-full"
                      onClick={() => handleImportCliCredentials(currentSubscription as 'claude' | 'codex')}
                    >
                      从 CLI 导入凭据
                    </Button>
                  )}
                  {isLoggedIn && (
                    <div className="space-y-1 text-xs" style={{ color: 'var(--accent-green)' }}>
                      <div className="flex items-center gap-1.5">
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        <span>{editForm.auth?.account_name || '已登录'}</span>
                      </div>
                      {editForm.auth?.account_email && (
                        <p className="ml-5" style={{ color: 'var(--fg-tertiary)' }}>{editForm.auth.account_email}</p>
                      )}
                    </div>
                  )}
                  {loginError && (
                    <p className="text-xs" style={{ color: 'var(--accent-red)' }}>{loginError}</p>
                  )}
                </div>
              )}

              {/* 无订阅时显示 API 配置 */}
              {!isSubscribed && (
                <>
                  <div className="h-px" style={{ background: 'var(--border)' }} />
                  <div className="space-y-2">
                    <Label>API Key</Label>
                    <Input type="password" value={editForm.api_key || ''} onChange={(e) => setEditForm(f => ({ ...f, api_key: e.target.value }))} placeholder="输入 API Key" />
                  </div>
                  <div className="space-y-2">
                    <Label>Base URL</Label>
                    <Input value={editForm.base_url || ''} onChange={(e) => setEditForm(f => ({ ...f, base_url: e.target.value }))} placeholder="https://api.example.com/v1" />
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-2">
                      <Label>Organization</Label>
                      <Input value={editForm.organization || ''} onChange={(e) => setEditForm(f => ({ ...f, organization: e.target.value }))} placeholder="可选" />
                    </div>
                    <div className="space-y-2">
                      <Label>Project</Label>
                      <Input value={editForm.project || ''} onChange={(e) => setEditForm(f => ({ ...f, project: e.target.value }))} placeholder="可选" />
                    </div>
                  </div>
                  <div className="space-y-2">
                    <Label>模型列表 URL (可选)</Label>
                    <Input value={editForm.models_url_override || ''} onChange={(e) => setEditForm(f => ({ ...f, models_url_override: e.target.value }))} placeholder="留空则自动构造" />
                  </div>
                  <div className="space-y-2">
                    <Label>User-Agent (可选)</Label>
                    <Input value={editForm.custom_user_agent || ''} onChange={(e) => setEditForm(f => ({ ...f, custom_user_agent: e.target.value }))} placeholder="部分端点需要白名单 UA" />
                  </div>
                </>
              )}
            </>
            )}
            </div>

            {/* ── 右列：模型管理 + 额度 ── */}
            <div className="space-y-4">
              <div className="space-y-2">
                <Label>模型列表</Label>
                {isReverseProxy ? (
                  <Button variant="outline" size="sm" onClick={handleRefreshModels} disabled={editFetchingModels} className="w-full">
                    {editFetchingModels ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                    {editFetchingModels ? '同步中...' : '同步本地模型'}
                  </Button>
                ) : (
                  <>
                    <div className="flex gap-2">
                      <Input value={editNewModelInput} onChange={(e) => setEditNewModelInput(e.target.value)} placeholder="输入模型名称" onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleEditAddModel(); } }} className="flex-1" />
                      <Button variant="outline" onClick={handleEditAddModel}>添加</Button>
                    </div>
                    <Button variant="outline" size="sm" onClick={handleRefreshModels} disabled={editFetchingModels} className="w-full">
                      {editFetchingModels ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                      {editFetchingModels ? '获取中...' : '从 API 获取模型列表'}
                    </Button>
                  </>
                )}
                {editForm.models.length > 0 ? (
                  <div className="flex flex-col gap-1 mt-2 max-h-[300px] overflow-y-auto p-2 rounded-lg custom-scrollbar" style={{ border: '0.5px solid var(--border)' }}>
                    {editForm.models.map((model) => {
                      const hidden = editForm.hidden_models?.includes(model);
                      return (
                        <div
                          key={model}
                          className={cn('hover-row flex items-center justify-between px-2 py-1.5 rounded text-[13px]', hidden && 'is-muted')}
                        >
                          <span style={hidden ? { textDecoration: 'line-through', color: 'var(--fg-tertiary)' } : { color: 'var(--fg-85)' }}>{model}</span>
                          <TextTooltip content={hidden ? '显示此模型' : '隐藏此模型'}>
                            <button
                              className="w-6 h-6 flex items-center justify-center rounded cursor-pointer bg-transparent border-none"
                              onClick={() => toggleModelHidden(model)}
                              style={{ color: 'var(--icon-tertiary)' }}
                              aria-label={hidden ? '显示此模型' : '隐藏此模型'}
                            >
                              {hidden ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                            </button>
                          </TextTooltip>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                    {isReverseProxy ? '暂无模型，点击"同步本地模型"从本地 server 拉取' : '暂无模型，请添加或点击"获取列表"'}
                  </p>
                )}
              </div>

              {/* 额度显示 */}
              {isSubscribed && (
                <div className="p-3 rounded-lg text-xs space-y-2" style={{ background: 'var(--bg-button-secondary)', border: '0.5px solid var(--border)' }}>
                  <div className="flex items-center justify-between">
                    <span className="font-medium" style={{ color: 'var(--fg-85)' }}>订阅额度</span>
                    <button
                      className="cursor-pointer"
                      style={{ color: 'var(--icon-accent)', background: 'none', border: 'none' }}
                      onClick={() => handleFetchQuota()}
                      disabled={quotaLoading}
                    >
                      {quotaLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                    </button>
                  </div>
                  {quotaInfo ? (
                    <QuotaDisplay quota={quotaInfo} />
                  ) : (
                    <p style={{ color: 'var(--fg-tertiary)' }}>{quotaLoading ? '查询中...' : '未查询'}</p>
                  )}
                </div>
              )}
            </div>
          </div>
          <DialogFooter className="flex justify-between">
            {isEditMode && !isReverseProxy ? (
              <Button
                variant="ghost"
                size="sm"
                className="text-destructive hover:text-destructive"
                style={{ color: 'var(--accent-red)' }}
                onClick={() => editProviderId && setDeleteConfirmId(editProviderId)}
              >
                <Trash2 className="h-4 w-4 mr-1" />
                删除
              </Button>
            ) : <div />}
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setEditDialogOpen(false)}>取消</Button>
              <Button onClick={handleSaveProvider} disabled={editSaving || (!isEditMode && !editNameInput.trim())}>
                {editSaving && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
                {isEditMode ? '保存' : '添加'}
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Delete Confirm Dialog ── */}
      <Dialog open={!!deleteConfirmId} onOpenChange={() => setDeleteConfirmId(null)}>
        <DialogContent className="max-w-[360px]">
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
            <DialogDescription>
              确定要删除 &ldquo;{deleteConfirmId ? getProviderDisplayName(deleteConfirmId) : ''}&rdquo; 吗？此操作不可撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteConfirmId(null)}>取消</Button>
            <Button variant="destructive" onClick={() => deleteConfirmId && handleDeleteProvider(deleteConfirmId)}>确认删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── OAuth 登录中弹窗 ── */}
      <Dialog open={!!loginHandle} onOpenChange={(open) => { if (!open) handleCancelLogin(); }}>
        <DialogContent className="max-w-[440px]">
          <DialogHeader>
            <DialogTitle>订阅登录</DialogTitle>
            <DialogDescription>请在浏览器中完成授权</DialogDescription>
          </DialogHeader>
          {loginHandle && (
            <div className="space-y-4 py-2">
              <div className="space-y-2">
                <Label>授权链接</Label>
                <div className="flex items-center gap-2">
                  <Input readOnly value={loginHandle.verification_uri} className="flex-1 text-xs" />
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => window.open(loginHandle.verification_uri, '_blank')}
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
              <div className="space-y-2">
                <Label>用户代码</Label>
                <div
                  className="text-2xl font-mono font-bold text-center py-4 rounded-lg tracking-[0.3em]"
                  style={{ background: 'var(--bg-button-secondary)', border: '0.5px solid var(--border)', color: 'var(--fg-85)' }}
                >
                  {loginHandle.user_code}
                </div>
                <button
                  className="w-full text-xs text-center cursor-pointer"
                  style={{ color: 'var(--icon-accent)', background: 'none', border: 'none' }}
                  onClick={() => { navigator.clipboard.writeText(loginHandle.user_code); toast.success('已复制'); }}
                >
                  点击复制
                </button>
              </div>
              {loginPolling && (
                <div className="flex items-center justify-center gap-2 text-sm" style={{ color: 'var(--fg-secondary)' }}>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  等待授权完成...
                </div>
              )}
              {loginError && (
                <p className="text-sm text-center" style={{ color: 'var(--accent-red)' }}>{loginError}</p>
              )}
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={handleCancelLogin}>取消</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ── 额度格式化显示 ──
function QuotaDisplay({ quota }: { quota: Record<string, unknown> }) {
  const sub = typeof quota.subscription === 'string' ? quota.subscription : '';
  if (sub === 'codex' && Array.isArray(quota.windows)) {
    return (
      <div className="space-y-2">
        {quota.windows.map((window, index) => {
          const record = typeof window === 'object' && window !== null
            ? window as Record<string, unknown>
            : {};
          const tier = typeof record.tier === 'string' ? record.tier : '窗口';
          const usedPercent = typeof record.used_percent === 'number' ? record.used_percent : 0;
          return (
            <div key={index} className="space-y-1">
              <div className="flex items-center justify-between">
                <span style={{ color: 'var(--fg-secondary)' }}>{tier}</span>
                <span style={{ color: usedPercent >= 80 ? 'var(--accent-red)' : 'var(--accent-green)' }}>
                  {usedPercent}%
                </span>
              </div>
              <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--bg-button-secondary)' }}>
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${Math.min(usedPercent, 100)}%`,
                    background: usedPercent >= 80 ? 'var(--accent-red)' : 'var(--accent-green)',
                  }}
                />
              </div>
              <p style={{ color: 'var(--fg-tertiary)' }}>重置: {formatResetTime(record.reset_at)}</p>
            </div>
          );
        })}
      </div>
    );
  }
  if (sub === 'copilot') {
    const plan = typeof quota.plan === 'string' ? quota.plan : '';
    const snapshots = typeof quota.quota_snapshots === 'object' && quota.quota_snapshots !== null
      ? quota.quota_snapshots as Record<string, unknown>
      : null;
    return (
      <div className="space-y-1">
        {plan && (
          <div className="flex items-center justify-between">
            <span style={{ color: 'var(--fg-secondary)' }}>套餐</span>
            <span style={{ color: 'var(--fg-85)' }}>{plan}</span>
          </div>
        )}
        {snapshots && Object.keys(snapshots).length > 0 && (
          <pre className="whitespace-pre-wrap break-all" style={{ color: 'var(--fg-tertiary)', fontSize: '11px', margin: 0 }}>
            {JSON.stringify(snapshots, null, 2)}
          </pre>
        )}
      </div>
    );
  }
  if (sub === 'claude' && typeof quota.windows === 'object' && quota.windows !== null) {
    const windows = quota.windows as Record<string, unknown>;
    const keys = Object.keys(windows);
    if (keys.length === 0) return <p style={{ color: 'var(--fg-tertiary)' }}>无额度数据</p>;
    return (
      <div className="space-y-1">
        {keys.map(k => {
          const value = windows[k];
          if (!value || typeof value !== 'object') return null;
          const window = value as Record<string, unknown>;
          const used = typeof window.limit_percent === 'number'
            ? window.limit_percent
            : typeof window.percent === 'number'
              ? window.percent
              : 0;
          return (
            <div key={k} className="flex items-center justify-between">
              <span style={{ color: 'var(--fg-secondary)' }}>{k}</span>
              <span style={{ color: used >= 80 ? 'var(--accent-red)' : 'var(--accent-green)' }}>{used}%</span>
            </div>
          );
        })}
      </div>
    );
  }
  // fallback
  return (
    <pre className="whitespace-pre-wrap break-all" style={{ color: 'var(--fg-tertiary)', fontSize: '11px', margin: 0 }}>
      {JSON.stringify(quota, null, 2)}
    </pre>
  );
}
