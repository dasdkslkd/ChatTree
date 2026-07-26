import { useEffect, useState, useCallback, useMemo } from 'react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { TextTooltip } from '@/components/ui/text-tooltip';
import { Textarea } from '@/components/ui/textarea';
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
import {
  Settings, StickyNote, Plus, Trash2, Eye, EyeOff,
  Loader2, Save, Pencil, Server, Wrench, Link2, RefreshCw,
  Boxes, Sparkles, Bot, Package, MessageSquare, FolderOpen, History,
  type LucideIcon,
} from 'lucide-react';
import { toast } from 'sonner';
import { configApi } from '../api/config';
import { createLauncherApi, type LauncherProfileStatus } from '../api/launcher';
import { modelApi } from '../api/model';
import { getProfileContext } from '../runtime/profileContext';
import { buildFrontendRoute } from '../runtime/profileRoute';
import { useNavigationStore } from '../store/navigationStore';
import { usePromptStore } from '../store/promtStore';
import type {
  BuiltinCodeToolGroup,
  BuiltinToolExposure,
  ConfigData,
  ModelProviderConfig,
  APIFormat,
  McpServerConfig,
  McpTransport,
  ToolsConfig,
  ToolInventoryStatus,
  BuiltinWebStatus,
  CapabilityInventory,
  CapabilityPlugin,
  ProjectCapabilityConfig,
  ProjectSettingsItem,
} from '../types/model';
import type { ToolPermissionMode } from '../types/message';
import type { Prompt, PromptResponse } from '../types/prompt';

/* ─── Constants ─── */

type SettingsSection = 'providers' | 'projects' | 'ssh' | 'prompts' | 'builtin_tools' | 'skills' | 'mcp' | 'agents' | 'plugins';

const SETTINGS_NAV: { key: SettingsSection; label: string; icon: typeof Settings; group: string }[] = [
  { key: 'providers', label: '供应商', icon: Server, group: '应用' },
  { key: 'projects', label: '项目', icon: FolderOpen, group: '应用' },
  { key: 'ssh', label: 'SSH Hosts', icon: Link2, group: '应用' },
  { key: 'builtin_tools', label: '内置工具', icon: Wrench, group: '工具与能力' },
  { key: 'skills', label: 'Skill', icon: Sparkles, group: '工具与能力' },
  { key: 'mcp', label: 'MCP', icon: Link2, group: '工具与能力' },
  { key: 'agents', label: 'Agent', icon: Bot, group: '工具与能力' },
  { key: 'plugins', label: '插件', icon: Package, group: '工具与能力' },
  { key: 'prompts', label: '提示词', icon: StickyNote, group: '应用' },
];

const API_FORMAT_OPTIONS: { value: APIFormat; label: string; description: string }[] = [
  { value: 'chat_completions', label: 'Chat Completions', description: 'OpenAI 兼容格式' },
  { value: 'responses', label: 'Responses API', description: 'OpenAI Responses API' },
  { value: 'anthropic', label: 'Anthropic', description: 'Anthropic Messages API' },
  { value: 'gemini', label: 'Gemini', description: 'Google Gemini API' },
];

const BUILTIN_EXPOSURE_OPTIONS: { value: BuiltinToolExposure; label: string; description: string }[] = [
  { value: 'coding', label: 'Coding', description: '代码读写、搜索、命令和网页工具' },
  { value: 'minimal', label: 'Minimal', description: '仅基础工具和网页工具' },
  { value: 'full', label: 'Full', description: '暴露完整 canonical 工具面' },
];

const BUILTIN_CODE_GROUP_OPTIONS: { value: BuiltinCodeToolGroup; label: string; description: string }[] = [
  { value: 'read', label: '读取', description: 'glob, read' },
  { value: 'search', label: '搜索', description: 'grep' },
  { value: 'edit', label: '编辑', description: 'edit' },
  { value: 'shell', label: '命令', description: 'shell' },
  { value: 'write', label: '写入', description: 'write' },
];

const TOOL_PERMISSION_MODE_OPTIONS: { value: ToolPermissionMode; label: string; description: string }[] = [
  { value: 'auto_approve', label: '自动批准', description: '除显式删除外自动执行工具' },
  { value: 'modify_only', label: '修改前询问', description: '读取自动执行，修改需确认' },
  { value: 'ask_always', label: '总是询问', description: '每次工具调用都需确认' },
  { value: 'plan', label: '计划模式', description: '仅允许只读规划工具' },
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

const DEFAULT_MCP_SERVER: McpServerConfig = {
  enabled: true,
  transport: 'streamable_http',
  url: 'http://localhost:3001',
  endpoint: 'http://localhost:3001',
  bearer_token: '',
  headers: {},
  command: '',
  args: [],
  stdio_framing: 'jsonl',
  env: {},
  cwd: '',
  timeout: 30,
  startup_timeout: 30,
  tool_call_timeout: 120,
  heartbeat_enabled: true,
  heartbeat_interval: 30,
  auto_start: undefined,
  auto_reconnect: true,
  max_reconnect_attempts: 3,
  http_retries: 2,
  http_retry_backoff: 0.5,
  enabled_tools: null,
  disabled_tools: [],
};

const DEFAULT_TOOLS_CONFIG: ToolsConfig = {
  enabled: true,
  max_rounds: 5,
  max_result_length: 8000,
  default_permission_mode: 'auto_approve',
  builtin: {
    enabled: true,
    exposure: 'coding',
    code: {
      enabled: true,
      groups: ['read', 'search', 'edit', 'shell'],
    },
  },
  web_search: {
    enabled: true,
    searxng: {
      searxng_url: 'http://localhost:8888',
      language: 'zh-CN',
      max_results: 10,
      timeout: 15,
    },
  },
  mcp: {
    enabled: false,
    servers: {},
  },
};

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9一-鿿]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

/* ─── Props ─── */

interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultSection?: SettingsSection;
}

/* ─── Component ─── */

export function SettingsPageView({ defaultSection = 'providers' }: { defaultSection?: SettingsSection }) {
  const [section, setSection] = useState<SettingsSection>(defaultSection);
  const { openChat } = useNavigationStore();

  useEffect(() => {
    setSection(defaultSection);
  }, [defaultSection]);

  return (
    <div className="flex h-full overflow-hidden" style={{ background: 'var(--bg-surface)' }}>
      {/* Left nav */}
      <nav
        className="app-sidebar flex-shrink-0"
        style={{ width: '300px' }}
      >
        <div className="app-sidebar-topbar">
          <span className="text-base font-semibold" style={{ color: 'var(--fg-85)' }}>设置</span>
        </div>

        <div className="flex-1 overflow-y-auto p-2 custom-scrollbar">
          {(() => {
            const groups: Record<string, typeof SETTINGS_NAV> = {};
            SETTINGS_NAV.forEach(item => {
              if (!groups[item.group]) groups[item.group] = [];
              groups[item.group].push(item);
            });
            return Object.entries(groups).map(([group, items]) => (
              <div key={group}>
                <div
                  className="app-sidebar-project-heading"
                  style={{ letterSpacing: '0.025em' }}
                >
                  {group}
                </div>
                <div className="flex flex-col gap-1">
                  {items.map(item => {
                    const Icon = item.icon;
                    const isActive = section === item.key;
                    return (
                      <button
                        type="button"
                        key={item.key}
                        className={cn('app-sidebar-action', isActive && 'is-active')}
                        onClick={() => setSection(item.key)}
                      >
                        <Icon
                          className="w-4 h-4 flex-shrink-0"
                          style={{ color: isActive ? 'var(--icon-accent)' : 'var(--icon-tertiary)' }}
                        />
                        <span>{item.label}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ));
          })()}
        </div>
        <div className="app-sidebar-footer">
          <TextTooltip content="返回对话">
            <button
              type="button"
              className="app-sidebar-action"
              onClick={openChat}
            >
              <MessageSquare className="h-4 w-4 shrink-0" />
              <span>返回对话</span>
            </button>
          </TextTooltip>
        </div>
      </nav>

      {/* Content */}
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        {section === 'providers' && <ProvidersSection />}
        {section === 'projects' && <ProjectsSection />}
        {section === 'ssh' && <SshHostsSection />}
        {section === 'builtin_tools' && <BuiltinToolsSection />}
        {section === 'skills' && <CapabilitiesSection view="skills" />}
        {section === 'mcp' && <McpSection />}
        {section === 'agents' && <CapabilitiesSection view="agents" />}
        {section === 'plugins' && <CapabilitiesSection view="plugins" />}
        {section === 'prompts' && <PromptsSection />}
      </div>
    </div>
  );
}

export function SettingsDialog({ open, onOpenChange, defaultSection = 'providers' }: SettingsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="p-0 gap-0 overflow-hidden border-0"
        style={{
          width: '90vw',
          maxWidth: '860px',
          height: '80vh',
          maxHeight: '640px',
          background: 'var(--bg-elevated)',
          border: '0.5px solid var(--border)',
          borderRadius: '20px',
          boxShadow: 'var(--shadow-2xl)',
        }}
      >
        <SettingsPageView defaultSection={defaultSection} />
      </DialogContent>
    </Dialog>
  );
}

/* ─── SSH Hosts Section ─── */

function SshHostsSection() {
  const [path, setPath] = useState('');
  const [text, setText] = useState('');
  const [hosts, setHosts] = useState<string[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [statuses, setStatuses] = useState<Record<string, LauncherProfileStatus>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyHost, setBusyHost] = useState<string | null>(null);

  const launcher = useMemo(
    () => createLauncherApi(getProfileContext(), window.location.href),
    [],
  );

  const refreshStatuses = useCallback(async (hostAliases: string[]) => {
    const entries = await Promise.all(
      hostAliases.map(async (host) => {
        try {
          const response = await launcher.getSshHostStatus(host);
          return [host, response.session] as const;
        } catch {
          return [host, null] as const;
        }
      }),
    );
    setStatuses(() => {
      const next: Record<string, LauncherProfileStatus> = {};
      for (const [host, status] of entries) {
        if (status) next[host] = status;
      }
      return next;
    });
  }, [launcher]);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const snapshot = await launcher.getSshConfig();
      setPath(snapshot.path);
      setText(snapshot.text);
      setHosts(snapshot.hosts);
      setWarnings(snapshot.warnings);
      await refreshStatuses(snapshot.hosts);
    } catch {
      toast.error('加载 SSH 配置失败');
    } finally {
      setLoading(false);
    }
  }, [launcher, refreshStatuses]);

  useEffect(() => { void load(); }, [load]);

  const handleSave = async () => {
    try {
      setSaving(true);
      const snapshot = await launcher.saveSshConfig(text);
      setPath(snapshot.path);
      setText(snapshot.text);
      setHosts(snapshot.hosts);
      setWarnings(snapshot.warnings);
      await refreshStatuses(snapshot.hosts);
      toast.success('SSH config 已保存');
    } catch {
      toast.error('保存 SSH config 失败');
    } finally {
      setSaving(false);
    }
  };

  const connectHost = async (host: string) => {
    try {
      setBusyHost(host);
      if (window.electronAPI) {
        await window.electronAPI.connectSshHost(host);
        await refreshStatuses([host]);
        return;
      }
      const response = await launcher.connectSshHost(host);
      setStatuses((current) => ({ ...current, [host]: response.session }));
      window.location.href = buildFrontendRoute({ profileId: response.profile_id });
    } catch {
      toast.error(`连接 ${host} 失败`);
    } finally {
      setBusyHost(null);
    }
  };

  const disconnectHost = async (host: string) => {
    try {
      setBusyHost(host);
      const response = await launcher.disconnectSshHost(host);
      setStatuses((current) => ({ ...current, [host]: response.session }));
      toast.success(`${host} 已断开`);
    } catch {
      toast.error(`断开 ${host} 失败`);
    } finally {
      setBusyHost(null);
    }
  };

  const openHost = (host: string) => {
    const status = statuses[host];
    if (!status || status.status !== 'ready') return;
    if (window.electronAPI) {
      void window.electronAPI.connectSshHost(host);
      return;
    }
    window.location.href = buildFrontendRoute({ profileId: status.profile_id });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载中...</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>SSH Hosts</h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>{path || '~/.ssh/config'}</p>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6 space-y-5">
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <Label className="text-sm" style={{ color: 'var(--fg-85)' }}>~/.ssh/config</Label>
            <Button size="sm" onClick={handleSave} disabled={saving}>
              {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
              保存
            </Button>
          </div>
          <Textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            className="min-h-[220px] resize-y font-mono text-xs leading-relaxed"
            spellCheck={false}
          />
          {warnings.length > 0 && (
            <div className="space-y-1 text-xs" style={{ color: 'var(--accent-yellow)' }}>
              {warnings.map((warning) => <div key={warning}>{warning}</div>)}
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label className="text-sm" style={{ color: 'var(--fg-85)' }}>Host</Label>
            <Button variant="outline" size="sm" onClick={load}>
              <RefreshCw className="h-4 w-4 mr-1" />
              刷新
            </Button>
          </div>

          {hosts.length === 0 ? (
            <div
              className="px-3 py-8 text-center text-sm rounded-lg"
              style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}
            >
              暂无可连接 Host
            </div>
          ) : (
            <div className="space-y-2">
              {hosts.map((host) => {
                const status = statuses[host];
                const ready = status?.status === 'ready';
                const busy = busyHost === host || status?.status === 'connecting';
                return (
                  <div
                    key={host}
                    className="flex items-center gap-3 rounded-lg px-3 py-2"
                    style={{ border: '0.5px solid var(--border)' }}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{host}</div>
                      <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        {status?.status ?? 'disconnected'}
                        {status?.phase ? ` · ${status.phase}` : ''}
                      </div>
                    </div>
                    <Button variant="outline" size="sm" onClick={() => openHost(host)} disabled={!ready}>
                      打开
                    </Button>
                    {ready ? (
                      <Button variant="outline" size="sm" onClick={() => disconnectHost(host)} disabled={busy}>
                        断开
                      </Button>
                    ) : (
                      <Button size="sm" onClick={() => connectHost(host)} disabled={busy}>
                        {busy ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Link2 className="h-4 w-4 mr-1" />}
                        连接
                      </Button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ─── Providers Section ─── */

function ProvidersSection() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [loading, setLoading] = useState(true);

  // Add provider dialog
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [newProviderName, setNewProviderName] = useState('');
  const [newProviderId, setNewProviderId] = useState('');
  const [newProviderFormat, setNewProviderFormat] = useState<APIFormat>('chat_completions');
  const [newProviderUrl, setNewProviderUrl] = useState('');
  const [newProviderKey, setNewProviderKey] = useState('');
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
    try {
      setAdding(true);
      await configApi.addProvider({ id, name, api_format: newProviderFormat, base_url: newProviderUrl, api_key: newProviderKey });
      toast.success(`"${name}" 已添加`);
      setAddDialogOpen(false);
      await loadConfig();
    } catch (err) {
      toast.error('添加失败');
    } finally {
      setAdding(false);
    }
  };

  const openEditDialog = (providerId: string) => {
    const cfg = config?.provider?.[providerId] ?? { ...DEFAULT_PROVIDER_CONFIG, name: providerId };
    setEditProviderId(providerId);
    setEditForm(sanitizeProviderConfig({ ...cfg, hidden_models: [...(cfg.hidden_models || [])] }));
    setEditNewModelInput('');
    setEditDialogOpen(true);
  };

  const handleFetchModels = async () => {
    if (!editProviderId) return;
    try {
      setEditFetchingModels(true);
      await configApi.update({ provider_configs: { [editProviderId]: sanitizeProviderConfig(editForm) } });
      const models = await modelApi.list(editProviderId);
      if (models?.length) {
        const merged = [...new Set([...editForm.models, ...models])];
        setEditForm(f => ({ ...f, models: merged }));
        toast.success(`获取到 ${models.length} 个模型`);
      } else {
        toast.error('未获取到模型');
      }
    } catch {
      toast.error('获取失败');
    } finally {
      setEditFetchingModels(false);
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
            <div className="space-y-2">
              <Label>Base URL (可选)</Label>
              <Input value={newProviderUrl} onChange={(e) => setNewProviderUrl(e.target.value)} placeholder="https://api.example.com/v1" />
            </div>
            <div className="space-y-2">
              <Label>API Key (可选)</Label>
              <Input type="password" value={newProviderKey} onChange={(e) => setNewProviderKey(e.target.value)} placeholder="可稍后填写" />
            </div>
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
                <Label>API Key</Label>
                <Input type="password" value={editForm.api_key || ''} onChange={(e) => setEditForm(f => ({ ...f, api_key: e.target.value }))} placeholder="输入 API Key" />
              </div>
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
            </div>

            {/* ── 右列：模型管理 ── */}
            <div className="space-y-4">
              <div className="space-y-2">
                <Label>模型列表</Label>
                <div className="flex gap-2">
                  <Input value={editNewModelInput} onChange={(e) => setEditNewModelInput(e.target.value)} placeholder="输入模型名称" onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleEditAddModel(); } }} className="flex-1" />
                  <Button variant="outline" onClick={handleEditAddModel}>添加</Button>
                </div>
                <Button variant="outline" size="sm" onClick={handleFetchModels} disabled={editFetchingModels} className="w-full">
                  {editFetchingModels ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : null}
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
    </div>
  );
}

/* ─── Projects Section ─── */

function ProjectsSection() {
  const [projects, setProjects] = useState<ProjectSettingsItem[]>([]);
  const [inventory, setInventory] = useState<CapabilityInventory | null>(null);
  const [mcpStatus, setMcpStatus] = useState<ToolInventoryStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [draft, setDraft] = useState<ProjectCapabilityConfig | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const loadProjects = useCallback(async () => {
    try {
      setLoading(true);
      const [projectData, capabilityData, statusData] = await Promise.all([
        configApi.getProjects(),
        configApi.getCapabilities(),
        configApi.getMcpStatus().catch(() => null),
      ]);
      setProjects(projectData.projects || []);
      setInventory(capabilityData);
      setMcpStatus(statusData);
      setSelectedPath(current => {
        if (current && projectData.projects.some(project => project.path === current)) return current;
        return projectData.projects[0]?.path || null;
      });
    } catch (err) {
      toast.error('加载项目配置失败: ' + (err instanceof Error ? err.message : ''));
      setProjects([]);
      setInventory(null);
      setMcpStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadProjects(); }, [loadProjects]);

  const selectedProject = projects.find(project => project.path === selectedPath) || null;
  useEffect(() => {
    if (!selectedProject) {
      setDraft(null);
      return;
    }
    setDraft({
      label: selectedProject.config?.label || selectedProject.label,
      visible: selectedProject.config?.visible !== false,
      enabled_skills: cloneOptionalList(selectedProject.config?.enabled_skills),
      enabled_mcp_servers: cloneOptionalList(selectedProject.config?.enabled_mcp_servers),
      enabled_agents: cloneOptionalList(selectedProject.config?.enabled_agents),
    });
  }, [selectedProject]);

  const skillNames = (inventory?.skills || []).map(skill => skill.name).sort((a, b) => a.localeCompare(b));
  const agentNames = (inventory?.agents || []).map(agent => agent.name).sort((a, b) => a.localeCompare(b));
  const mcpServerNames = Array.from(new Set((mcpStatus?.mcp_servers || []).map(server => server.name))).sort((a, b) => a.localeCompare(b));

  const updateDraft = (updater: (current: ProjectCapabilityConfig) => ProjectCapabilityConfig) => {
    setDraft(current => updater(current || {}));
  };

  const setAllowlistItem = (
    key: 'enabled_skills' | 'enabled_mcp_servers' | 'enabled_agents',
    allNames: string[],
    name: string,
    enabled: boolean,
  ) => {
    updateDraft(current => {
      const base = current[key] == null ? allNames : current[key] || [];
      const next = new Set(base);
      if (enabled) next.add(name);
      else next.delete(name);
      return { ...current, [key]: allNames.filter(item => next.has(item)) };
    });
  };

  const setAllowlistAll = (
    key: 'enabled_skills' | 'enabled_mcp_servers' | 'enabled_agents',
    allNames: string[],
    enabled: boolean,
  ) => {
    updateDraft(current => ({ ...current, [key]: enabled ? [...allNames] : [] }));
  };

  const handleSaveProject = async () => {
    if (!selectedProject || !draft) return;
    try {
      setSaving(true);
      await configApi.updateProject(selectedProject.path, {
        ...draft,
        label: draft.label || selectedProject.label,
      });
      toast.success('项目配置已保存');
      window.dispatchEvent(new Event('chattree-projects-updated'));
      await loadProjects();
    } catch (err) {
      toast.error('保存失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteHistory = async () => {
    if (!selectedProject) return;
    try {
      setDeleting(true);
      const result = await configApi.deleteProjectHistory(selectedProject.path, false);
      if (result.skipped_active_ids.length > 0) {
        toast.error(`已删除 ${result.deleted_count} 条，跳过 ${result.skipped_active_ids.length} 条运行中的对话`);
      } else {
        toast.success(`已删除 ${result.deleted_count} 条对话历史`);
      }
      setDeleteDialogOpen(false);
      window.dispatchEvent(new Event('chattree-projects-updated'));
      await loadProjects();
    } catch (err) {
      toast.error('删除失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setDeleting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载项目中...</span>
      </div>
    );
  }

  return (
    <div className="flex h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex w-[260px] flex-shrink-0 flex-col overflow-hidden" style={{ borderRight: '0.5px solid var(--border)' }}>
        <div className="flex-shrink-0 px-4 pt-5 pb-3">
          <h1 className="text-xl font-semibold" style={{ color: 'var(--fg-85)' }}>项目</h1>
          <p className="mt-1 text-xs" style={{ color: 'var(--fg-secondary)' }}>管理主页面显示与项目能力</p>
        </div>
        <div className="flex-1 overflow-y-auto p-2 custom-scrollbar">
          {projects.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>暂无项目</div>
          ) : projects.map(project => (
            <button
              key={project.path}
              type="button"
              className={cn('app-sidebar-action h-auto items-start py-2', selectedPath === project.path && 'is-active')}
              onClick={() => setSelectedPath(project.path)}
            >
              <FolderOpen className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="min-w-0 flex-1 text-left">
                <span className="block truncate">{project.label}</span>
                <span className="block truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
                  {project.conversation_count} 条 · {project.path}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {!selectedProject || !draft ? (
          <div className="flex flex-1 flex-col items-center justify-center text-sm" style={{ color: 'var(--fg-tertiary)' }}>
            <FolderOpen className="mb-3 h-10 w-10" />
            选择一个项目
          </div>
        ) : (
          <>
            <div className="flex-shrink-0 px-6 pt-6 pb-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h1 className="truncate text-2xl font-semibold" style={{ color: 'var(--fg-85)' }}>{selectedProject.label}</h1>
                  <p className="mt-1 truncate text-sm" style={{ color: 'var(--fg-secondary)' }}>{selectedProject.path}</p>
                </div>
                <Button variant="outline" size="sm" onClick={loadProjects} disabled={saving}>
                  <RefreshCw className="h-3.5 w-3.5 mr-1" />
                  刷新
                </Button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto px-6 pb-6 custom-scrollbar">
              <div className="max-w-[760px] space-y-4">
                <div className="rounded-xl p-4" style={{ border: '0.5px solid var(--border)' }}>
                  <div className="flex items-center justify-between gap-4">
                    <div>
                      <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>主页面显示</div>
                      <div className="mt-1 text-xs" style={{ color: 'var(--fg-tertiary)' }}>关闭后不会在主页面项目列表和新对话项目选择中显示</div>
                    </div>
                    <Switch
                      checked={draft.visible !== false}
                      onCheckedChange={(checked) => updateDraft(current => ({ ...current, visible: checked }))}
                    />
                  </div>
                </div>

                <ProjectAllowlistPanel
                  title="Skill"
                  icon={Sparkles}
                  names={skillNames}
                  value={draft.enabled_skills}
                  emptyText="暂无可配置 Skill"
                  onToggle={(name, enabled) => setAllowlistItem('enabled_skills', skillNames, name, enabled)}
                  onSetAll={(enabled) => setAllowlistAll('enabled_skills', skillNames, enabled)}
                />
                <ProjectAllowlistPanel
                  title="MCP"
                  icon={Link2}
                  names={mcpServerNames}
                  value={draft.enabled_mcp_servers}
                  emptyText="暂无已发现 MCP Server"
                  onToggle={(name, enabled) => setAllowlistItem('enabled_mcp_servers', mcpServerNames, name, enabled)}
                  onSetAll={(enabled) => setAllowlistAll('enabled_mcp_servers', mcpServerNames, enabled)}
                />
                <ProjectAllowlistPanel
                  title="Agent"
                  icon={Bot}
                  names={agentNames}
                  value={draft.enabled_agents}
                  emptyText="暂无可配置 Agent"
                  onToggle={(name, enabled) => setAllowlistItem('enabled_agents', agentNames, name, enabled)}
                  onSetAll={(enabled) => setAllowlistAll('enabled_agents', agentNames, enabled)}
                />

                <div className="rounded-xl p-4" style={{ border: '0.5px solid var(--destructive, #ef4444)' }}>
                  <div className="flex items-center justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 text-sm font-medium" style={{ color: 'var(--destructive, #ef4444)' }}>
                        <History className="h-4 w-4" />
                        对话历史
                      </div>
                      <div className="mt-1 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        删除此项目下的 {selectedProject.conversation_count} 条对话历史
                      </div>
                    </div>
                    <Button
                      variant="destructive"
                      size="sm"
                      disabled={selectedProject.conversation_count === 0}
                      onClick={() => setDeleteDialogOpen(true)}
                    >
                      <Trash2 className="h-4 w-4 mr-1" />
                      批量删除
                    </Button>
                  </div>
                </div>
              </div>
            </div>

            <div className="flex-shrink-0 flex justify-end gap-2 px-6 pb-5">
              <Button variant="outline" onClick={loadProjects} disabled={saving}>重置</Button>
              <Button onClick={handleSaveProject} disabled={saving}>
                {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
                保存
              </Button>
            </div>
          </>
        )}
      </div>

      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent className="max-w-[420px]">
          <DialogHeader>
            <DialogTitle>删除项目对话历史</DialogTitle>
            <DialogDescription>
              将删除此项目下的全部对话历史；运行中的任务会先请求停止。此操作不可撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)} disabled={deleting}>取消</Button>
            <Button variant="destructive" onClick={handleDeleteHistory} disabled={deleting}>
              {deleting ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Trash2 className="h-4 w-4 mr-1" />}
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ProjectAllowlistPanel({
  title,
  icon: Icon,
  names,
  value,
  emptyText,
  onToggle,
  onSetAll,
}: {
  title: string;
  icon: LucideIcon;
  names: string[];
  value?: string[] | null;
  emptyText: string;
  onToggle: (name: string, enabled: boolean) => void;
  onSetAll: (enabled: boolean) => void;
}) {
  const selected = value == null ? new Set(names) : new Set(value);
  const isolated = value != null;
  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
      <div className="flex items-center justify-between gap-3 px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
        <div className="flex min-w-0 items-center gap-2">
          <Icon className="h-4 w-4 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{title}</span>
          <span className="rounded-full px-1.5 py-0.5 text-xs" style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
            {selected.size}/{names.length}
          </span>
          <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
            {isolated ? '项目隔离' : '继承全部'}
          </span>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={() => onSetAll(true)}>全部</Button>
          <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={() => onSetAll(false)}>清空</Button>
        </div>
      </div>
      <div className="max-h-[220px] overflow-y-auto custom-scrollbar divide-y" style={{ '--tw-divide-color': 'var(--border-light, rgba(255,247,240,0.05))' } as React.CSSProperties}>
        {names.length === 0 ? (
          <div className="px-4 py-6 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>{emptyText}</div>
        ) : names.map(name => (
          <div key={name} className="flex items-center justify-between gap-3 px-4 py-2.5">
            <span className="min-w-0 truncate text-sm" style={{ color: 'var(--fg-secondary)' }}>{name}</span>
            <Switch checked={selected.has(name)} onCheckedChange={(checked) => onToggle(name, checked)} />
          </div>
        ))}
      </div>
    </div>
  );
}

function cloneOptionalList(value?: string[] | null): string[] | null {
  if (value == null) return null;
  return [...value];
}

/* ─── Capabilities Section ─── */

function CapabilitiesSection({ view }: { view: 'skills' | 'agents' | 'plugins' }) {
  const [inventory, setInventory] = useState<CapabilityInventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const meta = {
    skills: {
      title: 'Skill',
      description: '查看当前可用的技能',
      icon: Sparkles,
      empty: '暂无 Skill',
    },
    agents: {
      title: 'Agent',
      description: '查看当前可用的代理',
      icon: Bot,
      empty: '暂无 Agent',
    },
    plugins: {
      title: '插件',
      description: '查看当前可用的插件',
      icon: Package,
      empty: '暂无插件',
    },
  }[view];

  const loadCapabilities = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const nextInventory = await configApi.getCapabilities();
      setInventory(nextInventory);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载能力信息失败');
      setInventory(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadCapabilities(); }, [loadCapabilities]);

  const reloadCapabilities = useCallback(async () => {
    try {
      setReloading(true);
      setError(null);
      const nextInventory = await configApi.reloadCapabilities();
      setInventory(nextInventory);
      toast.success('能力已重载');
    } catch (err) {
      const message = err instanceof Error ? err.message : '重载能力失败';
      toast.error(message);
      if (!inventory) setError(message);
    } finally {
      setReloading(false);
    }
  }, [inventory]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载能力信息中...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
        <div className="flex-shrink-0 px-6 pt-6 pb-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>{meta.title}</h1>
              <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>{meta.description}</p>
            </div>
            <Button variant="outline" size="sm" className="mt-2 mr-10" onClick={reloadCapabilities} disabled={reloading}>
              <RefreshCw className={cn('h-3.5 w-3.5 mr-1', reloading && 'animate-spin')} />
              重载
            </Button>
          </div>
        </div>
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 pb-6 text-center">
          <Boxes className="h-9 w-9" style={{ color: 'var(--icon-tertiary)' }} />
          <div>
            <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>加载失败</div>
            <div className="mt-1 max-w-[420px] text-xs" style={{ color: 'var(--fg-tertiary)' }}>{error}</div>
          </div>
          <Button variant="outline" size="sm" className="mt-2 mr-10" onClick={reloadCapabilities} disabled={reloading}>
            <RefreshCw className={cn('h-3.5 w-3.5 mr-1', reloading && 'animate-spin')} />
            {reloading ? '重载中' : '重载'}
          </Button>
        </div>
      </div>
    );
  }

  const skills = inventory?.skills || [];
  const agents = inventory?.agents || [];
  const plugins = inventory?.plugins || [];
  const count = view === 'skills' ? skills.length : view === 'agents' ? agents.length : plugins.length;
  const EmptyIcon = meta.icon;

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>{meta.title}</h1>
            <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>{meta.description}</p>
          </div>
          <Button variant="outline" size="sm" onClick={reloadCapabilities} disabled={reloading}>
            <RefreshCw className={cn('h-3.5 w-3.5 mr-1', reloading && 'animate-spin')} />
            {reloading ? '重载中' : '重载'}
          </Button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6 space-y-4">
        <div className="grid grid-cols-1 gap-3">
          <CapabilityCountCard label={meta.title} value={count} icon={meta.icon} />
        </div>

        {count === 0 ? (
          <div className="rounded-xl px-4 py-10 text-center" style={{ border: '0.5px solid var(--border)' }}>
            <EmptyIcon className="mx-auto mb-3 h-9 w-9" style={{ color: 'var(--icon-tertiary)' }} />
            <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{meta.empty}</div>
          </div>
        ) : (
          <>
            {view === 'skills' && <CapabilityGroup title="Skill" count={skills.length} emptyText="暂无 Skill">
              {skills.map(skill => (
                <CapabilityItem
                  key={`${skill.source}:${skill.name}:${skill.path || ''}`}
                  icon={Sparkles}
                  name={skill.name}
                  description={skill.description || skill.when_to_use || '未提供描述'}
                  source={skill.source}
                  path={skill.path}
                  pluginId={skill.plugin_id}
                  pluginName={skill.plugin_name}
                  badges={skill.allowed_tools?.slice(0, 3)}
                />
              ))}
            </CapabilityGroup>}

            {view === 'agents' && <CapabilityGroup title="Agent" count={agents.length} emptyText="暂无 Agent">
              {agents.map(agent => (
                <CapabilityItem
                  key={`${agent.source}:${agent.name}:${agent.path || ''}`}
                  icon={Bot}
                  name={agent.name}
                  description={agent.description || '未提供描述'}
                  source={agent.source}
                  path={agent.path}
                  pluginId={agent.plugin_id}
                  pluginName={agent.plugin_name}
                  badges={[...(agent.skills || []), ...(agent.tools || [])].slice(0, 3)}
                />
              ))}
            </CapabilityGroup>}

            {view === 'plugins' && <CapabilityGroup title="插件" count={plugins.length} emptyText="暂无插件">
              {plugins.map(plugin => (
                <PluginCapabilityItem key={plugin.plugin_id} plugin={plugin} />
              ))}
            </CapabilityGroup>}
          </>
        )}
      </div>
    </div>
  );
}

function CapabilityCountCard({ label, value, icon: Icon }: { label: string; value: number; icon: typeof Settings }) {
  return (
    <div className="rounded-xl px-3 py-3" style={{ border: '0.5px solid var(--border)' }}>
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-xs" style={{ color: 'var(--fg-tertiary)' }}>{label}</div>
          <div className="mt-1 text-xl font-semibold" style={{ color: 'var(--fg-85)' }}>{value}</div>
        </div>
        <Icon className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--icon-accent)' }} />
      </div>
    </div>
  );
}

function CapabilityGroup({ title, count, emptyText, children }: {
  title: string;
  count: number;
  emptyText: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
      <div
        className="flex items-center justify-between px-4 py-2.5"
        style={{ background: 'var(--bg-elevated-secondary, rgba(255,247,240,0.035))', borderBottom: '0.5px solid var(--border)' }}
      >
        <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{title}</span>
        <span className="text-xs px-1.5 py-0.5 rounded-full" style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
          {count}
        </span>
      </div>
      <div className="divide-y" style={{ '--tw-divide-color': 'var(--border-light, rgba(255,247,240,0.05))' } as React.CSSProperties}>
        {count > 0 ? children : (
          <div className="px-4 py-6 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>
            {emptyText}
          </div>
        )}
      </div>
    </div>
  );
}

function CapabilityItem({
  icon: Icon,
  name,
  description,
  source,
  path,
  pluginId,
  pluginName,
  badges,
}: {
  icon: typeof Settings;
  name: string;
  description?: string;
  source?: string;
  path?: string | null;
  pluginId?: string | null;
  pluginName?: string | null;
  badges?: string[];
}) {
  return (
    <div className="flex gap-3 px-4 py-3">
      <Icon className="mt-0.5 h-4 w-4 flex-shrink-0" style={{ color: 'var(--icon-tertiary)' }} />
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="min-w-0 truncate text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{name}</span>
          {source && <SourceBadge source={source} />}
          {pluginName || pluginId ? <PluginBadge pluginName={pluginName} pluginId={pluginId} /> : null}
        </div>
        {description && (
          <div className="line-clamp-2 text-xs leading-relaxed" style={{ color: 'var(--fg-secondary)' }}>
            {description}
          </div>
        )}
        {path && (
          <TextTooltip content={path}>
            <div className="truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
              {path}
            </div>
          </TextTooltip>
        )}
        {badges?.length ? (
          <div className="flex flex-wrap gap-1 pt-0.5">
            {badges.map(badge => (
              <span key={badge} className="max-w-[160px] truncate rounded px-1.5 py-0.5 text-[11px]" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
                {badge}
              </span>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function PluginCapabilityItem({ plugin }: { plugin: CapabilityPlugin }) {
  const mcpCount = plugin.mcp_servers ? Object.keys(plugin.mcp_servers).length : 0;
  const detailParts = [
    plugin.version ? `版本 ${plugin.version}` : '',
    `${plugin.skill_roots?.length || 0} skills roots`,
    `${plugin.agent_roots?.length || 0} agents roots`,
    mcpCount ? `${mcpCount} MCP servers` : '',
  ].filter(Boolean);

  return (
    <CapabilityItem
      icon={Package}
      name={plugin.name || plugin.plugin_id}
      description={plugin.description || plugin.error || detailParts.join(' · ') || '未提供描述'}
      source="plugin"
      path={plugin.root}
      pluginId={plugin.plugin_id}
      pluginName={plugin.name}
      badges={detailParts.slice(0, 3)}
    />
  );
}

function SourceBadge({ source }: { source: string }) {
  return (
    <span className="rounded px-1.5 py-0.5 text-xs" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
      {sourceLabel(source)}
    </span>
  );
}

function PluginBadge({ pluginName, pluginId }: { pluginName?: string | null; pluginId?: string | null }) {
  const text = pluginName && pluginId ? `${pluginName} / ${pluginId}` : pluginName || pluginId || '';
  return (
    <TextTooltip content={text}>
      <span className="max-w-[220px] truncate rounded px-1.5 py-0.5 text-xs" style={{ background: 'var(--bg-button-secondary)', color: 'var(--fg-secondary)' }}>
        {text}
      </span>
    </TextTooltip>
  );
}

function sourceLabel(source: string): string {
  switch (source) {
    case 'system': return '系统';
    case 'user': return '用户';
    case 'project': return '项目';
    case 'plugin': return '插件';
    default: return source;
  }
}

/* ─── MCP Section ─── */

function normalizeToolsConfig(raw?: ToolsConfig): ToolsConfig {
  return {
    ...DEFAULT_TOOLS_CONFIG,
    ...(raw || {}),
    builtin: {
      ...(DEFAULT_TOOLS_CONFIG.builtin || {}),
      ...(raw?.builtin || {}),
      code: {
        ...(DEFAULT_TOOLS_CONFIG.builtin?.code || {}),
        ...(raw?.builtin?.code || {}),
      },
    },
    web_search: {
      ...(DEFAULT_TOOLS_CONFIG.web_search || {}),
      ...(raw?.web_search || {}),
      searxng: {
        ...(DEFAULT_TOOLS_CONFIG.web_search?.searxng || {}),
        ...(raw?.web_search?.searxng || {}),
      },
    },
    mcp: {
      ...(DEFAULT_TOOLS_CONFIG.mcp || {}),
      ...(raw?.mcp || {}),
      servers: { ...(raw?.mcp?.servers || {}) },
    },
  };
}

function BuiltinToolsSection() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [toolsForm, setToolsForm] = useState<ToolsConfig>(normalizeToolsConfig());
  const [runtimeStatus, setRuntimeStatus] = useState<ToolInventoryStatus | null>(null);
  const [webStatus, setWebStatus] = useState<BuiltinWebStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [checkingWeb, setCheckingWeb] = useState(false);

  const updateTools = (updater: (current: ToolsConfig) => ToolsConfig) => {
    setToolsForm(current => normalizeToolsConfig(updater(normalizeToolsConfig(current))));
  };

  const setDefaultPermissionMode = (mode: ToolPermissionMode) => {
    updateTools(current => ({ ...current, default_permission_mode: mode }));
  };

  const loadRuntimeStatus = useCallback(async (options: { checkWeb?: boolean } = {}) => {
    try {
      const inventory = await configApi.getMcpStatus();
      setRuntimeStatus(inventory);
    } catch {
      setRuntimeStatus(null);
    }
    if (options.checkWeb === false) return;
    try {
      const web = await configApi.getBuiltinWebStatus();
      setWebStatus(web);
    } catch {
      setWebStatus(null);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      setConfig(data);
      setToolsForm(normalizeToolsConfig(data.tools));
      void loadRuntimeStatus({ checkWeb: true });
    } catch {
      toast.error('加载内置工具配置失败');
    } finally {
      setLoading(false);
    }
  }, [loadRuntimeStatus]);

  useEffect(() => {
    loadConfig();
    const timer = window.setInterval(() => { void loadRuntimeStatus({ checkWeb: false }); }, 5000);
    return () => window.clearInterval(timer);
  }, [loadConfig, loadRuntimeStatus]);

  const setBuiltinExposure = (exposure: BuiltinToolExposure) => {
    updateTools(current => ({
      ...current,
      builtin: {
        ...(current.builtin || {}),
        exposure,
      },
    }));
  };

  const setBuiltinCodeEnabled = (enabled: boolean) => {
    updateTools(current => ({
      ...current,
      builtin: {
        ...(current.builtin || {}),
        code: {
          ...(current.builtin?.code || {}),
          enabled,
        },
      },
    }));
  };

  const setBuiltinCodeGroup = (group: BuiltinCodeToolGroup, enabled: boolean) => {
    updateTools(current => {
      const groups = new Set(current.builtin?.code?.groups || []);
      if (enabled) groups.add(group);
      else groups.delete(group);
      return {
        ...current,
        builtin: {
          ...(current.builtin || {}),
          code: {
            ...(current.builtin?.code || {}),
            groups: Array.from(groups),
          },
        },
      };
    });
  };

  const setWebSearchEnabled = (enabled: boolean) => {
    updateTools(current => ({
      ...current,
      web_search: {
        ...(current.web_search || {}),
        enabled,
      },
    }));
  };

  const setSearxngField = (key: 'searxng_url' | 'language' | 'engines' | 'max_results' | 'timeout', value: string | number) => {
    updateTools(current => ({
      ...current,
      web_search: {
        ...(current.web_search || {}),
        searxng: {
          ...(current.web_search?.searxng || {}),
          [key]: value,
        },
      },
    }));
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      const committedTools = normalizeToolsConfig(toolsForm);
      setToolsForm(committedTools);
      await configApi.update({ tools: committedTools });
      toast.success('内置工具配置已保存');
      await loadConfig();
    } catch (err) {
      toast.error('保存失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setSaving(false);
    }
  };

  const handleCheckWeb = async () => {
    try {
      setCheckingWeb(true);
      const committedTools = normalizeToolsConfig(toolsForm);
      await configApi.update({ tools: committedTools });
      setToolsForm(committedTools);
      const nextStatus = await configApi.getBuiltinWebStatus();
      setWebStatus(nextStatus);
      if (nextStatus.available) toast.success('SearXNG 连接可用');
      else toast.error(nextStatus.error || 'SearXNG 不可用');
    } catch (err) {
      toast.error('检查失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setCheckingWeb(false);
    }
  };

  const webView = getBuiltinWebStatusView(webStatus, toolsForm);
  const searxng = toolsForm.web_search?.searxng || {};

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载内置工具配置中...</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>内置工具</h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>配置本地内置工具、代码工具分组和联网搜索能力</p>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6 space-y-4">
        <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <Label>工具系统</Label>
              <Switch checked={toolsForm.enabled !== false} onCheckedChange={(checked) => updateTools(current => ({ ...current, enabled: checked }))} />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">默认审批</Label>
              <Select
                value={toolsForm.default_permission_mode || 'auto_approve'}
                onValueChange={(value) => setDefaultPermissionMode(value as ToolPermissionMode)}
              >
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {TOOL_PERMISSION_MODE_OPTIONS.map(option => (
                    <SelectItem key={option.value} value={option.value} textValue={option.label}>
                      <div className="flex flex-col">
                        <span>{option.label}</span>
                        <span className="text-xs opacity-70">{option.description}</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">最大轮次</Label>
              <Input type="number" min={1} value={toolsForm.max_rounds ?? 5} onChange={(e) => updateTools(current => ({ ...current, max_rounds: parseNumber(e.target.value, 5) }))} />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">结果上限</Label>
              <Input type="number" min={1000} value={toolsForm.max_result_length ?? 8000} onChange={(e) => updateTools(current => ({ ...current, max_result_length: parseNumber(e.target.value, 8000) }))} />
            </div>
          </div>
        </div>

        <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
          <div className="flex items-center justify-between px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
            <div>
              <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>本地工具暴露</div>
              <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                可见 {runtimeStatus?.model_visible_tools?.length ?? 0} 个
                {runtimeStatus?.hidden_local_tools?.length ? `，隐藏 ${runtimeStatus.hidden_local_tools.length} 个` : ''}
              </div>
            </div>
            <Switch
              checked={toolsForm.builtin?.enabled !== false}
              onCheckedChange={(checked) => updateTools(current => ({
                ...current,
                builtin: { ...(current.builtin || {}), enabled: checked },
              }))}
            />
          </div>
          <div className="grid grid-cols-[220px_1fr] gap-4 px-4 py-3">
            <div className="space-y-2">
              <Label className="text-xs">暴露模式</Label>
              <Select
                value={toolsForm.builtin?.exposure || 'coding'}
                onValueChange={(value) => setBuiltinExposure(value as BuiltinToolExposure)}
              >
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {BUILTIN_EXPOSURE_OPTIONS.map(option => (
                    <SelectItem key={option.value} value={option.value} textValue={option.label}>
                      <div className="flex flex-col">
                        <span>{option.label}</span>
                        <span className="text-xs opacity-70">{option.description}</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label className="text-xs">代码工具分组</Label>
                <Switch
                  checked={toolsForm.builtin?.code?.enabled !== false}
                  onCheckedChange={setBuiltinCodeEnabled}
                />
              </div>
              <div className="grid grid-cols-5 gap-2">
                {BUILTIN_CODE_GROUP_OPTIONS.map(option => {
                  const enabledGroups = toolsForm.builtin?.code?.groups || [];
                  const enabled = enabledGroups.includes(option.value);
                  return (
                    <TextTooltip key={option.value} content={option.description}>
                      <button
                        type="button"
                        className="rounded-lg px-2 py-2 text-left text-xs transition-colors"
                        style={{
                          border: '0.5px solid var(--border)',
                          color: enabled ? 'var(--fg-85)' : 'var(--fg-tertiary)',
                          background: enabled ? 'var(--bg-button-secondary)' : 'transparent',
                        }}
                        onClick={() => setBuiltinCodeGroup(option.value, !enabled)}
                      >
                        <div className="font-medium">{option.label}</div>
                        <div className="truncate opacity-70">{option.description}</div>
                      </button>
                    </TextTooltip>
                  );
                })}
              </div>
            </div>
          </div>
        </div>

        <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
          <div className="flex items-center justify-between px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
            <div>
              <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>联网工具</div>
              <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>web</div>
            </div>
            <div className="flex items-center gap-3">
              <TextTooltip content={webView.title}>
                <span className="flex items-center gap-1.5 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                  <span className="h-2 w-2 rounded-full" style={{ background: webView.color }} />
                  {webView.label}
                </span>
              </TextTooltip>
              <Switch checked={toolsForm.web_search?.enabled !== false} onCheckedChange={setWebSearchEnabled} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4 px-4 py-3">
            <div className="space-y-2 col-span-2">
              <Label>SearXNG URL</Label>
              <Input
                value={searxng.searxng_url || ''}
                onChange={(e) => setSearxngField('searxng_url', e.target.value)}
                placeholder="http://localhost:8888"
              />
            </div>
            <div className="space-y-2">
              <Label>语言</Label>
              <Input value={searxng.language || 'zh-CN'} onChange={(e) => setSearxngField('language', e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>搜索引擎</Label>
              <Input value={searxng.engines || ''} onChange={(e) => setSearxngField('engines', e.target.value)} placeholder="留空使用 SearXNG 默认" />
            </div>
            <div className="space-y-2">
              <Label>最大结果数</Label>
              <Input type="number" min={1} max={20} value={searxng.max_results ?? 10} onChange={(e) => setSearxngField('max_results', parseNumber(e.target.value, 10))} />
            </div>
            <div className="space-y-2">
              <Label>超时秒数</Label>
              <Input type="number" min={1} value={searxng.timeout ?? 15} onChange={(e) => setSearxngField('timeout', parseNumber(e.target.value, 15))} />
            </div>
            {webStatus?.error && (
              <TextTooltip content={webStatus.error}>
                <div className="col-span-2 truncate text-xs" style={{ color: webStatus.available ? 'var(--fg-tertiary)' : 'var(--destructive, #ef4444)' }}>
                  {webStatus.error}
                </div>
              </TextTooltip>
            )}
          </div>
        </div>
      </div>

      <div className="flex-shrink-0 flex justify-end gap-2 px-6 pb-5">
        <Button variant="outline" onClick={loadConfig} disabled={saving || checkingWeb}>重置</Button>
        <Button variant="outline" onClick={handleCheckWeb} disabled={saving || checkingWeb || !config}>
          {checkingWeb ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-1" />}
          检查联网
        </Button>
        <Button onClick={handleSave} disabled={saving || !config}>
          {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
          保存
        </Button>
      </div>
    </div>
  );
}

function getBuiltinWebStatusView(webStatus: BuiltinWebStatus | null, toolsForm: ToolsConfig) {
  if (toolsForm.enabled === false || toolsForm.builtin?.enabled === false || toolsForm.web_search?.enabled === false) {
    return { label: '已禁用', color: 'var(--fg-tertiary)', title: '联网工具已禁用' };
  }
  if (!webStatus) {
    return { label: '未检查', color: 'var(--fg-tertiary)', title: '尚未获取 SearXNG 状态' };
  }
  if (webStatus.available) {
    return { label: '可用', color: 'var(--accent-green)', title: `${webStatus.searxng_url} 可用` };
  }
  return { label: '不可用', color: 'var(--destructive, #ef4444)', title: webStatus.error || `${webStatus.searxng_url} 不可用` };
}

function parseNumber(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function listToText(value?: string[] | null): string {
  return Array.isArray(value) ? value.join('\n') : '';
}

function textToList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map(item => item.trim())
    .filter(Boolean);
}

function textToArgs(value: string): string[] {
  const matches = value.match(/"[^"]*"|'[^']*'|\S+/g) || [];
  return matches.map(item => item.replace(/^(['"])(.*)\1$/, '$2')).filter(Boolean);
}

function recordToText(value?: Record<string, string>): string {
  return Object.entries(value || {}).map(([key, val]) => `${key}=${val}`).join('\n');
}

function textToRecord(value: string): Record<string, string> {
  const record: Record<string, string> = {};
  value.split('\n').forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const separator = trimmed.includes('=') ? '=' : ':';
    const index = trimmed.indexOf(separator);
    if (index <= 0) return;
    const key = trimmed.slice(0, index).trim();
    const val = trimmed.slice(index + 1).trim();
    if (key) record[key] = val;
  });
  return record;
}

function commandToString(value?: string | string[]): string {
  if (Array.isArray(value)) return value[0] || '';
  return value || '';
}

function argsToList(server: McpServerConfig): string[] {
  if (Array.isArray(server.args)) return server.args;
  if (Array.isArray(server.command)) return server.command.slice(1);
  return [];
}

function McpSection() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [toolsForm, setToolsForm] = useState<ToolsConfig>(normalizeToolsConfig());
  const [runtimeStatus, setRuntimeStatus] = useState<ToolInventoryStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [connectingServerId, setConnectingServerId] = useState<string | null>(null);
  const [selectedServerId, setSelectedServerId] = useState<string | null>(null);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [newServerId, setNewServerId] = useState('');
  const [newServerTransport, setNewServerTransport] = useState<McpTransport>('streamable_http');
  const [argsDraft, setArgsDraft] = useState('');
  const [envDraft, setEnvDraft] = useState('');
  const [headersDraft, setHeadersDraft] = useState('');

  const loadRuntimeStatus = useCallback(async () => {
    try {
      const status = await configApi.getMcpStatus();
      setRuntimeStatus(status);
    } catch {
      setRuntimeStatus(null);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      const normalized = normalizeToolsConfig(data.tools);
      const serverIds = Object.keys(normalized.mcp?.servers || {});
      setConfig(data);
      setToolsForm(normalized);
      setSelectedServerId(current => current && serverIds.includes(current) ? current : null);
      await loadRuntimeStatus();
    } catch {
      toast.error('加载 MCP 配置失败');
    } finally {
      setLoading(false);
    }
  }, [loadRuntimeStatus]);

  useEffect(() => {
    loadConfig();
    const timer = window.setInterval(loadRuntimeStatus, 5000);
    return () => window.clearInterval(timer);
  }, [loadConfig, loadRuntimeStatus]);

  const servers = toolsForm.mcp?.servers || {};
  const serverIds = Object.keys(servers);
  const selectedServer = selectedServerId ? servers[selectedServerId] : null;
  const runtimeByServer = new Map((runtimeStatus?.mcp_servers || []).map(server => [server.name, server]));

  const loadServerDrafts = (server: McpServerConfig) => {
    setArgsDraft(listToText(argsToList(server)));
    setEnvDraft(recordToText(server.env));
    setHeadersDraft(recordToText(server.headers));
  };

  const updateTools = (updater: (current: ToolsConfig) => ToolsConfig) => {
    setToolsForm(current => normalizeToolsConfig(updater(normalizeToolsConfig(current))));
  };

  const withCommittedDrafts = (base: ToolsConfig): ToolsConfig => {
    if (!selectedServerId) return base;
    const currentServer = base.mcp?.servers?.[selectedServerId];
    if (!currentServer) return base;
    return normalizeToolsConfig({
      ...base,
      mcp: {
        ...(base.mcp || {}),
        servers: {
          ...(base.mcp?.servers || {}),
          [selectedServerId]: {
            ...DEFAULT_MCP_SERVER,
            ...currentServer,
            args: textToArgs(argsDraft),
            env: textToRecord(envDraft),
            headers: textToRecord(headersDraft),
          },
        },
      },
    });
  };

  const commitDraftsToForm = () => {
    setToolsForm(current => withCommittedDrafts(normalizeToolsConfig(current)));
  };

  const setMcpEnabled = (enabled: boolean) => {
    updateTools(current => ({
      ...current,
      mcp: { ...(current.mcp || {}), enabled },
    }));
  };

  const setServerField = <K extends keyof McpServerConfig>(key: K, value: McpServerConfig[K]) => {
    if (!selectedServerId) return;
    updateTools(current => ({
      ...current,
      mcp: {
        ...(current.mcp || {}),
        servers: {
          ...(current.mcp?.servers || {}),
          [selectedServerId]: {
            ...DEFAULT_MCP_SERVER,
            ...(current.mcp?.servers || {})[selectedServerId],
            [key]: value,
          },
        },
      },
    }));
  };

  const handleOpenAddDialog = () => {
    setNewServerId('');
    setNewServerTransport('streamable_http');
    setAddDialogOpen(true);
  };

  const handleAddServer = () => {
    const id = newServerId.trim();
    if (!id) { toast.error('请输入 Server ID'); return; }
    if (servers[id]) { toast.error(`Server "${id}" 已存在`); return; }
    const newServer: McpServerConfig = {
      ...DEFAULT_MCP_SERVER,
      transport: newServerTransport,
      url: newServerTransport === 'streamable_http' ? DEFAULT_MCP_SERVER.url : '',
      endpoint: newServerTransport === 'streamable_http' ? DEFAULT_MCP_SERVER.endpoint : '',
      command: newServerTransport === 'stdio' ? 'npx' : '',
      args: newServerTransport === 'stdio' ? ['-y'] : [],
      auto_start: newServerTransport === 'stdio' ? false : true,
    };
    updateTools(current => ({
      ...current,
      mcp: {
        ...(current.mcp || {}),
        enabled: true,
        servers: {
          ...(current.mcp?.servers || {}),
          [id]: newServer,
        },
      },
    }));
    setSelectedServerId(id);
    loadServerDrafts(newServer);
    setEditDialogOpen(true);
    setAddDialogOpen(false);
  };

  const handleDeleteServer = () => {
    if (!selectedServerId) return;
    updateTools(current => {
      const nextServers = { ...(current.mcp?.servers || {}) };
      delete nextServers[selectedServerId];
      return {
        ...current,
        mcp: {
          ...(current.mcp || {}),
          servers: nextServers,
        },
      };
    });
    const nextId = serverIds.find(id => id !== selectedServerId) || null;
    setSelectedServerId(nextId);
    setEditDialogOpen(false);
  };

  const openServerDialog = (id: string) => {
    const server = servers[id];
    if (server) loadServerDrafts(server);
    setSelectedServerId(id);
    setEditDialogOpen(true);
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      const committedTools = withCommittedDrafts(toolsForm);
      setToolsForm(committedTools);
      await configApi.update({ tools: committedTools });
      await loadRuntimeStatus();
      toast.success('工具配置已保存');
      await loadConfig();
    } catch (err) {
      toast.error('保存失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setSaving(false);
    }
  };

  const handleConnectServer = async (id: string) => {
    try {
      setConnectingServerId(id);
      const committedTools = withCommittedDrafts(toolsForm);
      setToolsForm(committedTools);
      await configApi.update({ tools: committedTools });
      const status = await configApi.connectMcpServer(id);
      setRuntimeStatus(status);
      const serverStatus = status.mcp_servers.find(server => server.name === id);
      if (serverStatus?.connected) {
        toast.success(`MCP Server "${id}" 已连接`);
      } else {
        toast.error(serverStatus?.error || `MCP Server "${id}" 连接失败`);
      }
    } catch (err) {
      toast.error('连接失败: ' + (err instanceof Error ? err.message : ''));
      await loadRuntimeStatus();
    } finally {
      setConnectingServerId(null);
    }
  };

  const handleDisconnectServer = async (id: string) => {
    try {
      setConnectingServerId(id);
      const status = await configApi.disconnectMcpServer(id);
      setRuntimeStatus(status);
      toast.success(`MCP Server "${id}" 已断开`);
    } catch (err) {
      toast.error('断开失败: ' + (err instanceof Error ? err.message : ''));
      await loadRuntimeStatus();
    } finally {
      setConnectingServerId(null);
    }
  };

  const handleToggleServerConnection = async (id: string) => {
    if (runtimeByServer.get(id)?.connected) {
      await handleDisconnectServer(id);
      return;
    }
    await handleConnectServer(id);
  };

  const getServerStatusView = (id: string, server: McpServerConfig) => {
    const status = runtimeByServer.get(id);
    if (server.enabled === false) {
      return { label: '已禁用', color: 'var(--fg-tertiary)', title: '此 Server 已禁用' };
    }
    if (status?.connected) {
      return { label: '运行中', color: 'var(--accent-green)', title: `运行中，${status.tools_count ?? 0} 个工具` };
    }
    if (status?.error) {
      return { label: '连接失败', color: 'var(--destructive, #ef4444)', title: status.error };
    }
    if (server.transport === 'stdio' && status?.auto_start === false) {
      return { label: '未启动', color: 'var(--fg-tertiary)', title: 'stdio MCP 默认不随 Server 启动，可手动连接' };
    }
    return { label: '未连接', color: 'var(--fg-tertiary)', title: '尚未建立运行时连接' };
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载中...</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>MCP</h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>配置 MCP Server 与连接状态</p>
      </div>

      <div className="flex-1 overflow-hidden px-6 pb-6">
        <div className="flex h-full min-h-0 flex-col gap-4">
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="grid grid-cols-3 gap-4 px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>MCP</div>
                  <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>启用 MCP Server 工具</div>
                </div>
                <Switch checked={toolsForm.mcp?.enabled === true} onCheckedChange={setMcpEnabled} />
              </div>
              <div className="rounded-lg px-3 py-2" style={{ border: '0.5px solid var(--border)' }}>
                <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>工具系统</div>
                <div className="mt-1 text-sm font-medium" style={{ color: toolsForm.enabled === false ? 'var(--destructive, #ef4444)' : 'var(--fg-85)' }}>
                  {toolsForm.enabled === false ? '已禁用' : '已启用'}
                </div>
              </div>
              <div className="rounded-lg px-3 py-2" style={{ border: '0.5px solid var(--border)' }}>
                <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>连接状态</div>
                <div className="mt-1 text-sm font-medium" style={{ color: 'var(--fg-85)' }}>
                  {(runtimeStatus?.mcp_servers || []).filter(server => server.connected).length} / {serverIds.length} 已连接
                </div>
              </div>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="flex items-center justify-between px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
              <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>Servers</span>
              <button
                className="flex h-7 items-center gap-1 rounded-lg border-0 px-2 text-xs"
                style={{ background: 'var(--bg-button-secondary)', color: 'var(--fg-secondary)' }}
                onClick={handleOpenAddDialog}
              >
                <Plus className="h-3.5 w-3.5" />
                添加
              </button>
            </div>

            <div className="flex-1 overflow-y-auto custom-scrollbar divide-y" style={{ '--tw-divide-color': 'var(--border-light, rgba(255,247,240,0.05))' } as React.CSSProperties}>
              {serverIds.length > 0 ? serverIds.map((id) => {
                const server = servers[id];
                const Icon = server.transport === 'stdio' ? Wrench : Link2;
                const statusView = getServerStatusView(id, server);
                const isConnecting = connectingServerId === id;
                return (
                  <div
                    key={id}
                    className="flex w-full items-center gap-3 border-0 bg-transparent px-4 py-3 text-left transition-colors"
                    style={{ color: 'var(--fg-secondary)' }}
                    onClick={() => openServerDialog(id)}
                    onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
                    onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'transparent'; }}
                  >
                    <Icon className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--icon-tertiary)' }} />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{id}</div>
                      <div className="truncate text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        {server.transport === 'stdio'
                          ? [commandToString(server.command), ...argsToList(server)].filter(Boolean).join(' ')
                          : (server.url || server.endpoint || '')}
                      </div>
                    </div>
                    <span className="text-xs px-1.5 py-0.5 rounded" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
                      {server.transport === 'stdio' ? 'stdio' : 'HTTP'}
                    </span>
                    <TextTooltip content={statusView.title}>
                      <span className="flex items-center gap-1.5 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        <span className="h-2 w-2 rounded-full" style={{ background: statusView.color }} />
                        {statusView.label}
                      </span>
                    </TextTooltip>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      disabled={isConnecting || toolsForm.enabled === false || toolsForm.mcp?.enabled !== true || server.enabled === false}
                      onClick={(e) => {
                        e.stopPropagation();
                        handleToggleServerConnection(id);
                      }}
                    >
                      {isConnecting ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5 mr-1" />}
                      {runtimeByServer.get(id)?.connected ? '断开' : '连接'}
                    </Button>
                  </div>
                );
              }) : (
                <div className="px-3 py-8 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                  暂无 MCP Server
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="flex-shrink-0 flex justify-end gap-2 px-6 pb-5">
        <Button variant="outline" onClick={loadConfig} disabled={saving}>重置</Button>
        <Button onClick={handleSave} disabled={saving || !config}>
          {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
          保存
        </Button>
      </div>

      <Dialog
        open={editDialogOpen}
        onOpenChange={(open) => {
          if (!open) commitDraftsToForm();
          setEditDialogOpen(open);
        }}
      >
        <DialogContent className="max-w-[760px] max-h-[85vh] overflow-y-auto custom-scrollbar">
          {selectedServer && selectedServerId ? (
            <>
              <DialogHeader>
                <DialogTitle>{selectedServerId}</DialogTitle>
                <DialogDescription>MCP Server 详细配置</DialogDescription>
              </DialogHeader>
              <div className="space-y-5 py-2">
                {(() => {
                  const statusView = getServerStatusView(selectedServerId, selectedServer);
                  const isConnecting = connectingServerId === selectedServerId;
                  return (
                    <div className="flex items-center justify-between rounded-lg px-3 py-2" style={{ border: '0.5px solid var(--border)' }}>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--fg-85)' }}>
                          <span className="h-2 w-2 rounded-full" style={{ background: statusView.color }} />
                          {statusView.label}
                        </div>
                        {statusView.title && (
                          <TextTooltip content={statusView.title}>
                            <div className="mt-1 truncate text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                              {statusView.title}
                            </div>
                          </TextTooltip>
                        )}
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={isConnecting || toolsForm.enabled === false || toolsForm.mcp?.enabled !== true || selectedServer.enabled === false}
                        onClick={() => handleToggleServerConnection(selectedServerId)}
                      >
                        {isConnecting ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                        {runtimeByServer.get(selectedServerId)?.connected ? '断开' : '连接'}
                      </Button>
                    </div>
                  );
                })()}

                <div className="flex items-center justify-between gap-3">
                  <Label>启用此 Server</Label>
                  <div className="flex items-center gap-2">
                    <Switch
                      checked={selectedServer.enabled !== false}
                      onCheckedChange={(checked) => setServerField('enabled', checked)}
                    />
                    <Button variant="ghost" size="sm" className="text-destructive hover:text-destructive" onClick={handleDeleteServer}>
                      <Trash2 className="h-4 w-4 mr-1" />
                      删除
                    </Button>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>传输类型</Label>
                    <Select
                      value={selectedServer.transport}
                      onValueChange={(value) => {
                        const transport = value as McpTransport;
                        setServerField('transport', transport);
                        if (transport === 'streamable_http' && !(selectedServer.url || selectedServer.endpoint)) {
                          setServerField('url', DEFAULT_MCP_SERVER.url);
                          setServerField('endpoint', DEFAULT_MCP_SERVER.endpoint);
                        }
                        if (transport === 'streamable_http') setServerField('auto_start', true);
                        if (transport === 'stdio') setServerField('auto_start', false);
                        if (transport === 'stdio' && !commandToString(selectedServer.command)) {
                          setServerField('command', 'npx');
                          setServerField('args', ['-y']);
                          setArgsDraft('-y');
                        }
                      }}
                    >
                      <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="streamable_http" textValue="HTTP">HTTP</SelectItem>
                        <SelectItem value="stdio" textValue="stdio">stdio</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-2">
                    <Label>工作目录</Label>
                    <Input value={selectedServer.cwd || ''} onChange={(e) => setServerField('cwd', e.target.value)} placeholder="可选" />
                  </div>
                </div>

                {selectedServer.transport === 'stdio' ? (
                  <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-2">
                      <Label>启动命令</Label>
                      <Input
                        value={commandToString(selectedServer.command)}
                        onChange={(e) => setServerField('command', e.target.value)}
                        placeholder="npx"
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>stdio framing</Label>
                      <Select
                        value={selectedServer.stdio_framing || 'jsonl'}
                        onValueChange={(value) => setServerField('stdio_framing', value as McpServerConfig['stdio_framing'])}
                      >
                        <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="content_length" textValue="Content-Length">Content-Length</SelectItem>
                          <SelectItem value="jsonl" textValue="JSONL">JSONL</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-2">
                      <Label>启动参数</Label>
                      <Textarea
                        value={argsDraft}
                        onChange={(e) => setArgsDraft(e.target.value)}
                        onBlur={() => setServerField('args', textToArgs(argsDraft))}
                        className="min-h-[92px] text-sm"
                        placeholder={'-y\n@modelcontextprotocol/server-filesystem\nD:\\Workspace'}
                      />
                    </div>
                    <div className="col-span-2 space-y-2">
                      <Label>环境变量</Label>
                      <Textarea
                        value={envDraft}
                        onChange={(e) => setEnvDraft(e.target.value)}
                        onBlur={() => setServerField('env', textToRecord(envDraft))}
                        className="min-h-[86px] text-sm"
                        placeholder={'TOKEN=xxx\nDEBUG=true'}
                      />
                    </div>
                  </div>
                ) : (
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label>URL</Label>
                      <Input
                        value={selectedServer.url || selectedServer.endpoint || ''}
                        onChange={(e) => {
                          setServerField('url', e.target.value);
                          setServerField('endpoint', e.target.value);
                        }}
                        placeholder="http://localhost:3001"
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>Bearer Token</Label>
                      <Input
                        type="password"
                        value={selectedServer.bearer_token || ''}
                        onChange={(e) => setServerField('bearer_token', e.target.value)}
                        placeholder="可选"
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>Header key-value</Label>
                      <Textarea
                        value={headersDraft}
                        onChange={(e) => setHeadersDraft(e.target.value)}
                        onBlur={() => setServerField('headers', textToRecord(headersDraft))}
                        className="min-h-[86px] text-sm"
                        placeholder={'X-Api-Key=xxx\nX-Client=ChatTree'}
                      />
                    </div>
                  </div>
                )}

                <div className="grid grid-cols-3 gap-4">
                  <div className="space-y-2">
                    <Label>请求超时</Label>
                    <Input type="number" min={1} value={selectedServer.timeout ?? 30} onChange={(e) => setServerField('timeout', parseNumber(e.target.value, 30))} />
                  </div>
                  <div className="space-y-2">
                    <Label>启动超时</Label>
                    <Input type="number" min={1} value={selectedServer.startup_timeout ?? 30} onChange={(e) => setServerField('startup_timeout', parseNumber(e.target.value, 30))} />
                  </div>
                  <div className="space-y-2">
                    <Label>工具超时</Label>
                    <Input type="number" min={1} value={selectedServer.tool_call_timeout ?? 120} onChange={(e) => setServerField('tool_call_timeout', parseNumber(e.target.value, 120))} />
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="rounded-lg p-3 space-y-3" style={{ border: '0.5px solid var(--border)' }}>
                    <div className="flex items-center justify-between">
                      <Label>心跳连接</Label>
                      <Switch checked={selectedServer.heartbeat_enabled !== false} onCheckedChange={(checked) => setServerField('heartbeat_enabled', checked)} />
                    </div>
                    <div className="space-y-2">
                      <Label className="text-xs">心跳间隔</Label>
                      <Input type="number" min={1} value={selectedServer.heartbeat_interval ?? 30} onChange={(e) => setServerField('heartbeat_interval', parseNumber(e.target.value, 30))} />
                    </div>
                  </div>

                  <div className="rounded-lg p-3 space-y-3" style={{ border: '0.5px solid var(--border)' }}>
                    <div className="flex items-center justify-between">
                      <Label>随 Server 启动</Label>
                      <Switch
                        checked={selectedServer.auto_start ?? selectedServer.transport !== 'stdio'}
                        onCheckedChange={(checked) => setServerField('auto_start', checked)}
                      />
                    </div>
                    <div className="flex items-center justify-between">
                      <Label>自动重连</Label>
                      <Switch checked={selectedServer.auto_reconnect !== false} onCheckedChange={(checked) => setServerField('auto_reconnect', checked)} />
                    </div>
                    <div className="space-y-2">
                      <Label className="text-xs">重连次数</Label>
                      <Input type="number" min={1} value={selectedServer.max_reconnect_attempts ?? 3} onChange={(e) => setServerField('max_reconnect_attempts', parseNumber(e.target.value, 3))} />
                    </div>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>HTTP 重试次数</Label>
                    <Input type="number" min={0} value={selectedServer.http_retries ?? 2} onChange={(e) => setServerField('http_retries', parseNumber(e.target.value, 2))} />
                  </div>
                  <div className="space-y-2">
                    <Label>重试退避秒数</Label>
                    <Input type="number" min={0} step={0.1} value={selectedServer.http_retry_backoff ?? 0.5} onChange={(e) => setServerField('http_retry_backoff', parseNumber(e.target.value, 0.5))} />
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>启用工具</Label>
                    <Textarea
                      value={listToText(selectedServer.enabled_tools)}
                      onChange={(e) => {
                        const list = textToList(e.target.value);
                        setServerField('enabled_tools', list.length > 0 ? list : null);
                      }}
                      className="min-h-[80px] text-sm"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>禁用工具</Label>
                    <Textarea
                      value={listToText(selectedServer.disabled_tools)}
                      onChange={(e) => setServerField('disabled_tools', textToList(e.target.value))}
                      className="min-h-[80px] text-sm"
                    />
                  </div>
                </div>
              </div>
              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => {
                    commitDraftsToForm();
                    setEditDialogOpen(false);
                  }}
                >
                  关闭
                </Button>
              </DialogFooter>
            </>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog open={addDialogOpen} onOpenChange={setAddDialogOpen}>
        <DialogContent className="max-w-[420px]">
          <DialogHeader>
            <DialogTitle>添加 MCP Server</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label>Server ID</Label>
              <Input value={newServerId} onChange={(e) => setNewServerId(e.target.value)} placeholder="filesystem" />
            </div>
            <div className="space-y-2">
              <Label>传输类型</Label>
              <Select value={newServerTransport} onValueChange={(value) => setNewServerTransport(value as McpTransport)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="streamable_http" textValue="HTTP">HTTP</SelectItem>
                  <SelectItem value="stdio" textValue="stdio">stdio</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddDialogOpen(false)}>取消</Button>
            <Button onClick={handleAddServer}>添加</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/* ─── Prompts Section ─── */

function PromptsSection() {
  const { prompts, currentPrompt, loading, loadPrompts, loadPrompt, savePrompt, deletePrompt, clearCurrentPrompt } = usePromptStore();

  const [editingPrompt, setEditingPrompt] = useState<Prompt | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [promptToDelete, setPromptToDelete] = useState<PromptResponse | null>(null);

  useEffect(() => { loadPrompts(); }, []);

  const handleSelectPrompt = async (prompt: PromptResponse) => {
    setIsNew(false);
    await loadPrompt(prompt.id);
  };

  useEffect(() => {
    if (currentPrompt && !isNew) {
      setEditingPrompt({ ...currentPrompt });
    }
  }, [currentPrompt]);

  const handleCreateNew = () => {
    clearCurrentPrompt();
    setEditingPrompt({ id: `prompt_${Date.now()}`, title: '新提示词', content: '' });
    setIsNew(true);
  };

  const handleSave = async () => {
    if (!editingPrompt || !editingPrompt.title.trim()) return;
    try {
      setSaving(true);
      await savePrompt(editingPrompt);
      toast.success('保存成功');
      setIsNew(false);
    } catch {
      // error handled by store
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!promptToDelete) return;
    try {
      await deletePrompt(promptToDelete.id);
      toast.success('已删除');
      if (editingPrompt?.id === promptToDelete.id) {
        setEditingPrompt(null);
        setIsNew(false);
      }
    } catch {
      // error handled by store
    } finally {
      setDeleteDialogOpen(false);
      setPromptToDelete(null);
    }
  };

  const handleCancel = () => {
    if (currentPrompt) setEditingPrompt({ ...currentPrompt });
    else setEditingPrompt(null);
    setIsNew(false);
  };

  return (
    <div className="flex h-full">
      {/* Left: prompt list */}
      <div
        className="flex flex-col flex-shrink-0 overflow-hidden"
        style={{ width: '220px', borderRight: '0.5px solid var(--border)' }}
      >
        <div
          className="flex items-center justify-between px-3 py-2.5 flex-shrink-0"
          style={{ borderBottom: '0.5px solid var(--border)' }}
        >
          <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>提示词</span>
          <button
            className="w-6 h-6 flex items-center justify-center rounded cursor-pointer bg-transparent border-none"
            style={{ color: 'var(--icon-tertiary)' }}
            onClick={handleCreateNew}
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
          >
            <Plus className="h-4 w-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-1.5 custom-scrollbar">
          {loading && prompts.length === 0 ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--icon-accent)' }} />
            </div>
          ) : prompts.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>
              暂无提示词
            </div>
          ) : (
            prompts.map((prompt) => (
              <div
                key={prompt.id}
                className="flex items-center justify-between px-2.5 py-1.5 rounded-lg cursor-pointer transition-colors group"
                style={{
                  background: currentPrompt?.id === prompt.id ? 'var(--bg-button-tertiary-active)' : undefined,
                  color: 'var(--fg-85)',
                }}
                onClick={() => handleSelectPrompt(prompt)}
                onMouseEnter={(e) => {
                  if (currentPrompt?.id !== prompt.id) (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)';
                }}
                onMouseLeave={(e) => {
                  if (currentPrompt?.id !== prompt.id) (e.currentTarget as HTMLElement).style.background = '';
                }}
              >
                <span className="flex-1 truncate text-sm">{prompt.title}</span>
                <button
                  className="w-5 h-5 flex items-center justify-center rounded cursor-pointer bg-transparent border-none opacity-0 group-hover:opacity-100 transition-opacity"
                  style={{ color: 'var(--icon-tertiary)' }}
                  onClick={(e) => {
                    e.stopPropagation();
                    setPromptToDelete(prompt);
                    setDeleteDialogOpen(true);
                  }}
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Right: editor */}
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        {/* Fixed header */}
        <div className="flex-shrink-0 px-6 pt-6 pb-4">
          <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>
            {editingPrompt ? (isNew ? '新建提示词' : '编辑提示词') : '提示词'}
          </h1>
          <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
            {editingPrompt ? '配置系统提示词' : '管理系统提示词'}
          </p>
        </div>

        {/* Scrollable content */}
        <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6">
          {editingPrompt ? (
            <div className="space-y-4 max-w-[500px]">
              <div className="space-y-2">
                <Label>标题</Label>
                <Input
                  value={editingPrompt.title}
                  onChange={(e) => setEditingPrompt({ ...editingPrompt, title: e.target.value })}
                  placeholder="输入提示词标题"
                />
              </div>
              <div className="space-y-2">
                <Label>内容</Label>
                <Textarea
                  value={editingPrompt.content}
                  onChange={(e) => setEditingPrompt({ ...editingPrompt, content: e.target.value })}
                  placeholder="输入系统提示词内容..."
                  className="min-h-[250px] resize-y text-sm leading-relaxed"
                />
              </div>
              <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                系统提示词将作为对话的上下文，指导 AI 的行为和回复风格。
              </p>
              <div className="flex gap-2 justify-end pt-2">
                <Button variant="outline" onClick={handleCancel}>取消</Button>
                <Button onClick={handleSave} disabled={saving || !editingPrompt.title.trim()}>
                  {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
                  {saving ? '保存中...' : '保存'}
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center h-full" style={{ color: 'var(--fg-tertiary)' }}>
              <Pencil className="h-10 w-10 mb-3" />
              <p className="text-sm">选择一个提示词进行编辑</p>
              <p className="text-xs mt-1">或点击 + 创建新的</p>
            </div>
          )}
        </div>

        {/* Footer spacer */}
        <div className="flex-shrink-0 h-2" />
      </div>

      {/* Delete confirm */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent className="max-w-[360px]">
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
          </DialogHeader>
          <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
            确定要删除 &ldquo;{promptToDelete?.title}&rdquo; 吗？
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)}>取消</Button>
            <Button variant="destructive" onClick={handleDelete}>删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
