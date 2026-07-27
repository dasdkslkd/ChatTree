import { useEffect, useState, useCallback, useRef } from 'react';
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
import { Settings, Plus, Trash2, Eye, EyeOff, Loader2, ExternalLink, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { configApi, type SubscriptionLoginHandle } from '@/api/config';
import type {
  ConfigData,
  ModelProviderConfig,
  APIFormat,
} from '@/types/model';

const API_FORMAT_OPTIONS: { value: APIFormat; label: string; description: string }[] = [
  { value: 'chat_completions', label: 'Chat Completions', description: 'OpenAI 兼容格式' },
  { value: 'responses', label: 'Responses API', description: 'OpenAI Responses API' },
  { value: 'anthropic', label: 'Anthropic', description: 'Anthropic Messages API' },
  { value: 'gemini', label: 'Gemini', description: 'Google Gemini API' },
];

// 订阅类型选项；codex/copilot 走 OAuth 登录，claude/gemini 走 CLI 凭据导入
const SUBSCRIPTION_OPTIONS: {
  value: '' | 'codex' | 'copilot' | 'claude' | 'gemini';
  label: string;
  description: string;
}[] = [
  { value: '', label: '无（API Key）', description: '使用 API Key 认证' },
  { value: 'codex', label: 'ChatGPT Plus/Pro', description: 'OAuth 设备码登录' },
  { value: 'copilot', label: 'GitHub Copilot', description: 'OAuth 设备码登录' },
  { value: 'claude', label: 'Claude (Anthropic)', description: '从 Claude CLI 导入' },
  { value: 'gemini', label: 'Gemini (Google)', description: '从 Gemini CLI 导入' },
];

const DEFAULT_PROVIDER_CONFIG: ModelProviderConfig = {
  name: '',
  models: [],
  api_key: '',
  base_url: '',
  organization: '',
  project: '',
  api_format: 'chat_completions',
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

export function ProvidersSection() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [loading, setLoading] = useState(true);

  // Add provider dialog
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [newProviderName, setNewProviderName] = useState('');
  const [newProviderId, setNewProviderId] = useState('');
  const [newProviderFormat, setNewProviderFormat] = useState<APIFormat>('chat_completions');
  const [newProviderUrl, setNewProviderUrl] = useState('');
  const [newProviderKey, setNewProviderKey] = useState('');
  const [newProviderSubscription, setNewProviderSubscription] = useState<'' | 'codex' | 'copilot' | 'claude' | 'gemini'>('');
  const [adding, setAdding] = useState(false);

  // Edit provider dialog
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editProviderId, setEditProviderId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<ModelProviderConfig>({ ...DEFAULT_PROVIDER_CONFIG });
  const [editNewModelInput, setEditNewModelInput] = useState('');
  const [editFetchingModels, setEditFetchingModels] = useState(false);
  const [editSaving, setEditSaving] = useState(false);

  // Delete confirm
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  // 订阅登录流程
  const [loginHandle, setLoginHandle] = useState<SubscriptionLoginHandle | null>(null);
  const [loginPolling, setLoginPolling] = useState(false);
  const [loginProviderId, setLoginProviderId] = useState<string | null>(null);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [enterpriseDomain, setEnterpriseDomain] = useState('');
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 额度查询
  const [quotaInfo, setQuotaInfo] = useState<Record<string, unknown> | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(false);

  const loadConfig = useCallback(async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      setConfig(data);
    } catch (err) {
      toast.error('加载配置失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadConfig(); }, [loadConfig]);

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

  const sanitizeProviderConfig = (provider: ModelProviderConfig): ModelProviderConfig => {
    const { default_model: _ignored, ...rest } = provider as ModelProviderConfig & { default_model?: string };
    return rest;
  };

  const getApiFormatLabel = (format: string): string => {
    return API_FORMAT_OPTIONS.find(f => f.value === format)?.label || format;
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
      toast.success('默认模型已更新');
    } catch {
      setConfig(c => c ? { ...c, default_model: prev } : c);
      toast.error('保存失败');
    }
  };

  const handleOpenAddDialog = () => {
    setNewProviderName('');
    setNewProviderId('');
    setNewProviderFormat('chat_completions');
    setNewProviderUrl('');
    setNewProviderKey('');
    setNewProviderSubscription('');
    setAddDialogOpen(true);
  };

  const handleNameChange = (name: string) => {
    setNewProviderName(name);
    if (!newProviderId || newProviderId === slugify(newProviderName)) {
      setNewProviderId(slugify(name));
    }
  };

  const handleAddProvider = async () => {
    const id = newProviderId.trim();
    const name = newProviderName.trim();
    if (!id || !name) { toast.error('请输入提供商名称'); return; }
    if (config?.provider?.[id]) { toast.error(`ID "${id}" 已存在`); return; }

    const sub = newProviderSubscription;
    // 订阅类型自动映射 api_format；选了订阅就不再要 api_key
    const formatMap: Record<string, APIFormat> = {
      codex: 'responses',
      copilot: 'chat_completions',
      claude: 'anthropic',
      gemini: 'gemini',
    };
    const api_format = sub ? formatMap[sub] : newProviderFormat;
    const auth = sub ? { type: 'oauth' as const, subscription: sub } : undefined;

    try {
      setAdding(true);
      await configApi.addProvider({
        id, name, api_format,
        base_url: newProviderUrl,
        api_key: sub ? '' : newProviderKey,
        auth,
      });
      toast.success(`"${name}" 已添加`);
      setAddDialogOpen(false);
      await loadConfig();
      // 选了订阅 → 自动打开编辑对话框让用户完成登录/导入
      if (sub) {
        openEditDialog(id, {
          ...DEFAULT_PROVIDER_CONFIG,
          name,
          api_format,
          base_url: newProviderUrl,
          auth: { type: 'oauth', subscription: sub },
        });
      }
    } catch (err) {
      toast.error('添加失败');
    } finally {
      setAdding(false);
    }
  };

  const openEditDialog = (providerId: string, providerConfig?: ModelProviderConfig) => {
    const cfg = providerConfig ?? config?.provider?.[providerId] ?? { ...DEFAULT_PROVIDER_CONFIG, name: providerId };
    setEditProviderId(providerId);
    setEditForm(sanitizeProviderConfig({ ...cfg, hidden_models: [...(cfg.hidden_models || [])] }));
    setEditNewModelInput('');
    setEditDialogOpen(true);
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
    if (!editProviderId) return;
    try {
      setEditSaving(true);
      const providerConfig = sanitizeProviderConfig(editForm);
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

  // ── 订阅登录：codex/copilot 走 OAuth 设备码 ──
  const handleStartSubscriptionLogin = async (subscription: 'codex' | 'copilot') => {
    if (!editProviderId) return;
    setLoginProviderId(editProviderId);
    setLoginError(null);
    setEnterpriseDomain('');
    try {
      const handle = await configApi.startSubscriptionLogin(
        editProviderId,
        subscription,
      );
      setLoginHandle(handle);
      setLoginPolling(true);
      // 自动打开浏览器
      window.open(handle.verification_uri, '_blank');
      // 开始轮询
      pollLogin(subscription, handle);
    } catch (e) {
      setLoginError(String(e) || '启动登录失败');
    }
  };

  const pollLogin = async (subscription: string, handle: SubscriptionLoginHandle) => {
    if (!loginProviderId) return;
    try {
      const result = await configApi.pollSubscriptionLogin(
        loginProviderId,
        subscription,
        handle,
      );
      if (result.status === 'ok') {
        setLoginPolling(false);
        setLoginHandle(null);
        toast.success('登录成功');
        await loadConfig();
        // 登录成功后自动拉取模型列表
        try {
          const { models } = await configApi.refreshProviderModels(loginProviderId);
          if (models?.length) {
            toast.success(`获取到 ${models.length} 个模型`);
            await loadConfig();
          }
        } catch {
          // 模型拉取失败不阻塞
        }
        // 登录成功后查询额度
        handleFetchQuota();
      } else {
        // pending — 继续轮询
        pollTimerRef.current = setTimeout(() => pollLogin(subscription, handle), (handle.interval || 5) * 1000);
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

  // ── CLI 凭据导入：claude/gemini ──
  const handleImportCliCredentials = async (subscription: 'claude' | 'gemini' | 'codex') => {
    if (!editProviderId) return;
    try {
      await configApi.importCliCredentials(editProviderId, subscription);
      toast.success('凭据导入成功');
      await loadConfig();
      // 导入后自动拉取模型列表（codex 订阅才有模型端点）
      if (subscription === 'codex') {
        try {
          const { models } = await configApi.refreshProviderModels(editProviderId);
          if (models?.length) {
            toast.success(`获取到 ${models.length} 个模型`);
            await loadConfig();
          }
        } catch {
          // 模型拉取失败不阻塞
        }
      }
      // 导入成功后查询额度
      handleFetchQuota();
    } catch {
      toast.error('未找到 CLI 凭据，请先在对应 CLI 工具中登录');
    }
  };

  // ── 额度查询 ──
  const handleFetchQuota = async () => {
    if (!editProviderId) return;
    try {
      setQuotaLoading(true);
      const quota = await configApi.getProviderQuota(editProviderId);
      setQuotaInfo(quota);
    } catch {
      toast.error('额度查询失败');
    } finally {
      setQuotaLoading(false);
    }
  };

  // ── 强制刷新模型列表 ──
  const handleRefreshModels = async () => {
    if (!editProviderId) return;
    try {
      setEditFetchingModels(true);
      const { models } = await configApi.refreshProviderModels(editProviderId);
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

  // 打开编辑对话框时，若有订阅则自动查询额度
  useEffect(() => {
    if (editDialogOpen && editProviderId) {
      const pc = config?.provider?.[editProviderId];
      if (pc?.auth?.subscription) {
        setQuotaInfo(null);
        handleFetchQuota();
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editDialogOpen, editProviderId]);

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
          style={{ background: 'var(--bg-elevated-secondary, rgba(255,247,240,0.035))', borderBottom: '0.5px solid var(--border)' }}
        >
          全局设置
        </div>
        <div className="px-4 py-3 grid gap-4 md:grid-cols-2">
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
        </div>
      </div>

      {/* Provider list */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: '0.5px solid var(--border)' }}
      >
        <div
          className="px-4 py-2.5 flex items-center justify-between"
          style={{ background: 'var(--bg-elevated-secondary, rgba(255,247,240,0.035))', borderBottom: '0.5px solid var(--border)' }}
        >
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>提供商</span>
            <span className="text-xs px-1.5 py-0.5 rounded-full" style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
              {enabledProviders.length}/{providerIds.length}
            </span>
          </div>
          <button
            className="flex items-center gap-1 text-xs px-2.5 py-1 rounded-lg cursor-pointer transition-colors"
            style={{ background: 'var(--bg-button-secondary, rgba(255,247,240,0.06))', color: 'var(--fg-secondary)', border: 'none' }}
            onClick={handleOpenAddDialog}
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-secondary-hover, rgba(255,247,240,0.10))'; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-secondary, rgba(255,247,240,0.06))'; }}
          >
            <Plus className="h-3.5 w-3.5" />
            添加
          </button>
        </div>
        <div className="divide-y" style={{ '--tw-divide-opacity': 1, '--tw-divide-color': 'var(--border-light, rgba(255,247,240,0.05))' } as React.CSSProperties}>
          {providerIds.length > 0 ? providerIds.map((pid) => {
            const pc = config!.provider[pid];
            return (
              <div
                key={pid}
                className="flex items-center justify-between px-4 py-2.5 cursor-pointer transition-colors"
                onClick={() => openEditDialog(pid)}
                onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
              >
                <div className="flex items-center gap-3">
                  <Settings className="h-4 w-4" style={{ color: 'var(--icon-tertiary)' }} />
                  <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{pc.name || pid}</span>
                  <span className="text-xs px-1.5 py-0.5 rounded" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
                    {getApiFormatLabel(pc.api_format)}
                  </span>
                  {pc.enabled && (
                    <span className="text-xs px-1.5 py-0.5 rounded-full" style={{ background: 'rgba(95,185,138,0.15)', color: 'var(--accent-green)' }}>
                      已启用
                    </span>
                  )}
                </div>
                <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                  {pc.models?.length || 0} 个模型
                </span>
              </div>
            );
          }) : (
            <div className="px-4 py-8 text-center" style={{ color: 'var(--fg-tertiary)' }}>
              <p className="text-sm">暂无已配置的提供商</p>
            </div>
          )}
        </div>
      </div>
      </div>

      {/* Footer spacer */}
      <div className="flex-shrink-0 h-2" />

      {/* ── Add Provider Dialog ── */}
      <Dialog open={addDialogOpen} onOpenChange={setAddDialogOpen}>
        <DialogContent className="max-w-[480px]">
          <DialogHeader>
            <DialogTitle>添加提供商</DialogTitle>
            <DialogDescription>配置一个新的 AI 模型提供商</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label>显示名称</Label>
              <Input value={newProviderName} onChange={(e) => handleNameChange(e.target.value)} placeholder="例如：My OpenAI" />
            </div>
            <div className="space-y-2">
              <Label>提供商 ID</Label>
              <Input value={newProviderId} onChange={(e) => setNewProviderId(e.target.value)} placeholder="自动生成" />
              <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>唯一标识，仅限英文、数字和连字符</p>
            </div>
            <div className="space-y-2">
              <Label>订阅类型</Label>
              <Select
                value={newProviderSubscription}
                onValueChange={(v) => {
                  const sub = v as '' | 'codex' | 'copilot' | 'claude' | 'gemini';
                  setNewProviderSubscription(sub);
                  // 选订阅时自动锁定 api_format；切回无订阅时恢复默认
                  if (sub === 'codex') setNewProviderFormat('responses');
                  else if (sub === 'copilot') setNewProviderFormat('chat_completions');
                  else if (sub === 'claude') setNewProviderFormat('anthropic');
                  else if (sub === 'gemini') setNewProviderFormat('gemini');
                }}
              >
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {SUBSCRIPTION_OPTIONS.map(opt => (
                    <SelectItem key={opt.value || 'none'} value={opt.value || 'none'} textValue={opt.label}>
                      <div className="flex flex-col">
                        <span>{opt.label}</span>
                        <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>{opt.description}</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {!newProviderSubscription && (
              <div className="space-y-2">
                <Label>API 格式</Label>
                <Select value={newProviderFormat} onValueChange={(v) => setNewProviderFormat(v as APIFormat)}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {API_FORMAT_OPTIONS.map(opt => (
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
            )}
            {!newProviderSubscription && (
              <div className="space-y-2">
                <Label>Base URL (可选)</Label>
                <Input value={newProviderUrl} onChange={(e) => setNewProviderUrl(e.target.value)} placeholder="https://api.example.com/v1" />
              </div>
            )}
            {!newProviderSubscription && (
              <div className="space-y-2">
                <Label>API Key (可选)</Label>
                <Input type="password" value={newProviderKey} onChange={(e) => setNewProviderKey(e.target.value)} placeholder="可稍后填写" />
              </div>
            )}
            {newProviderSubscription && (
              <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                添加后将在编辑对话框中完成登录或 CLI 凭据导入。
              </p>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddDialogOpen(false)}>取消</Button>
            <Button onClick={handleAddProvider} disabled={adding || !newProviderName.trim()}>
              {adding && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
              {adding ? '添加中...' : '确认添加'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Edit Provider Dialog ── */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent className="max-w-[780px] max-h-[85vh] overflow-y-auto custom-scrollbar">
          <DialogHeader>
            <DialogTitle>{editForm.name || editProviderId}</DialogTitle>
            <DialogDescription>配置提供商参数和模型列表</DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-2 gap-10 py-2">
            {/* ── 左列：基本配置 ── */}
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <Switch checked={editForm.enabled} onCheckedChange={(checked) => setEditForm(f => ({ ...f, enabled: checked }))} />
                <Label>启用此提供商</Label>
              </div>
              <div className="h-px" style={{ background: 'var(--border)' }} />
              <div className="space-y-2">
                <Label>API 格式</Label>
                <Select value={editForm.api_format || 'chat_completions'} onValueChange={(v) => setEditForm(f => ({ ...f, api_format: v as APIFormat }))}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {API_FORMAT_OPTIONS.map(opt => (
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
              <div className="h-px" style={{ background: 'var(--border)' }} />
              <div className="space-y-2">
                <Label>订阅类型</Label>
                <Select
                  value={editForm.auth?.subscription || ''}
                  onValueChange={(v) => {
                    const sub = v as '' | 'codex' | 'copilot' | 'claude' | 'gemini';
                    setEditForm(f => ({
                      ...f,
                      auth: sub ? { type: 'oauth', subscription: sub } : undefined,
                    }));
                    setQuotaInfo(null);
                  }}
                >
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {SUBSCRIPTION_OPTIONS.map(opt => (
                      <SelectItem key={opt.value || 'none'} value={opt.value || 'none'} textValue={opt.label}>
                        <div className="flex flex-col">
                          <span>{opt.label}</span>
                          <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>{opt.description}</span>
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {editForm.auth?.subscription && (
                  <div className="space-y-2 pt-1">
                    {editForm.auth.subscription === 'copilot' && (
                      <Input
                        value={enterpriseDomain}
                        onChange={(e) => setEnterpriseDomain(e.target.value)}
                        placeholder="企业版域名（可选，如 github.example.com）"
                      />
                    )}
                    {(editForm.auth.subscription === 'codex' || editForm.auth.subscription === 'copilot') && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-full"
                        onClick={() => handleStartSubscriptionLogin(editForm.auth!.subscription as 'codex' | 'copilot')}
                        disabled={loginPolling}
                      >
                        {loginPolling ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : null}
                        {loginPolling ? '等待授权...' : '登录'}
                      </Button>
                    )}
                    {(editForm.auth.subscription === 'claude' || editForm.auth.subscription === 'gemini' || editForm.auth.subscription === 'codex') && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-full"
                        onClick={() => handleImportCliCredentials(editForm.auth!.subscription as 'claude' | 'gemini' | 'codex')}
                      >
                        从 CLI 导入凭据
                      </Button>
                    )}
                    {editForm.auth.access && (
                      <p className="text-xs" style={{ color: 'var(--accent-green)' }}>已登录</p>
                    )}
                  </div>
                )}
              </div>
              <div className="h-px" style={{ background: 'var(--border)' }} />
              {!editForm.auth?.subscription && (
                <div className="space-y-2">
                  <Label>API Key</Label>
                  <Input type="password" value={editForm.api_key || ''} onChange={(e) => setEditForm(f => ({ ...f, api_key: e.target.value }))} placeholder="输入 API Key" />
                </div>
              )}
              <div className="space-y-2">
                <Label>Base URL</Label>
                <Input value={editForm.base_url || ''} onChange={(e) => setEditForm(f => ({ ...f, base_url: e.target.value }))} placeholder="https://api.example.com/v1" />
              </div>
              <div className="space-y-2">
                <Label>Organization (可选)</Label>
                <Input value={editForm.organization || ''} onChange={(e) => setEditForm(f => ({ ...f, organization: e.target.value }))} placeholder="组织 ID" />
              </div>
              <div className="space-y-2">
                <Label>Project (可选)</Label>
                <Input value={editForm.project || ''} onChange={(e) => setEditForm(f => ({ ...f, project: e.target.value }))} placeholder="项目 ID" />
              </div>
              <div className="h-px" style={{ background: 'var(--border)' }} />
              <div className="space-y-2">
                <Label>模型列表 URL (可选)</Label>
                <Input value={editForm.models_url_override || ''} onChange={(e) => setEditForm(f => ({ ...f, models_url_override: e.target.value }))} placeholder="留空则按 base_url 自动构造候选" />
                <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>覆写 /v1/models 端点；适用于智谱 /api/coding/paas/v4、Kimi 等非标准路径</p>
              </div>
              <div className="space-y-2">
                <Label>User-Agent (可选)</Label>
                <Input value={editForm.custom_user_agent || ''} onChange={(e) => setEditForm(f => ({ ...f, custom_user_agent: e.target.value }))} placeholder="部分端点（如 Kimi Coding Plan）需要白名单 UA" />
              </div>
            </div>

            {/* ── 右列：模型管理 ── */}
            <div className="space-y-4">
              <div className="space-y-2">
                <Label>模型列表</Label>
                <div className="flex gap-2">
                  <Input value={editNewModelInput} onChange={(e) => setEditNewModelInput(e.target.value)} placeholder="输入模型名称" onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleEditAddModel(); } }} className="flex-1" />
                  <Button variant="outline" onClick={handleEditAddModel}>添加</Button>
                </div>
                <Button variant="outline" size="sm" onClick={handleRefreshModels} disabled={editFetchingModels} className="w-full">
                  {editFetchingModels ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                  {editFetchingModels ? '获取中...' : '从 API 获取模型列表'}
                </Button>
                {editForm.models.length > 0 ? (
                  <div className="flex flex-col gap-1 mt-2 max-h-[240px] overflow-y-auto p-2 rounded-lg custom-scrollbar" style={{ border: '0.5px solid var(--border)' }}>
                    {editForm.models.map((model) => {
                      const hidden = editForm.hidden_models?.includes(model);
                      return (
                        <div
                          key={model}
                          className="flex items-center justify-between px-2 py-1.5 rounded text-[13px] transition-colors"
                          style={{ background: hidden ? 'var(--muted)' : 'transparent', opacity: hidden ? 0.6 : 1 }}
                          onMouseEnter={(e) => { if (!hidden) (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
                          onMouseLeave={(e) => { if (!hidden) (e.currentTarget as HTMLElement).style.background = ''; }}
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
                  <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>暂无模型，请添加或点击"获取列表"</p>
                )}
              </div>
              {editForm.auth?.subscription && quotaInfo && (
                <div className="p-3 rounded-lg text-xs space-y-1" style={{ background: 'var(--bg-elevated-secondary, rgba(255,247,240,0.035))', border: '0.5px solid var(--border)' }}>
                  <div className="flex items-center justify-between">
                    <span style={{ color: 'var(--fg-secondary)' }}>订阅额度</span>
                    <button
                      className="cursor-pointer"
                      style={{ color: 'var(--icon-accent)', background: 'none', border: 'none' }}
                      onClick={handleFetchQuota}
                      disabled={quotaLoading}
                    >
                      {quotaLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                    </button>
                  </div>
                  <pre className="whitespace-pre-wrap break-all" style={{ color: 'var(--fg-tertiary)', fontSize: '11px', margin: 0 }}>
                    {JSON.stringify(quotaInfo, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </div>
          <DialogFooter className="flex justify-between">
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
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setEditDialogOpen(false)}>取消</Button>
              <Button onClick={handleSaveProvider} disabled={editSaving}>
                {editSaving && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
                保存
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
                  style={{ background: 'var(--bg-elevated-secondary, rgba(255,247,240,0.035))', border: '0.5px solid var(--border)', color: 'var(--fg-85)' }}
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
